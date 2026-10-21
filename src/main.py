"""
АЛЬБИОН: Локатор-Навигатор (Автономная экспериментальная версия)
Версия: 4.2.1 — порт 8021, стабильная версия
"""
from __future__ import annotations

import asyncio
import base64
import difflib
import importlib
import io
import json
import math
import os
import re
import sqlite3
import sys
import time
import uuid
from fractions import Fraction
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import piexif
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from PIL import Image
from pydantic import BaseModel, Field, FiniteFloat, field_validator

# A local ignored .env is convenient for the single-user deployment. Explicit
# process environment values (e.g. systemd/CloudPub secrets) take precedence.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

from batch import router as batch_router

import auth

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

# --- ЯНДЕКС ГЕОКОДЕР И ПОДСКАЗКИ ---
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "")
YANDEX_SUGGEST_API_KEY = os.getenv("YANDEX_SUGGEST_API_KEY", "")
YANDEX_JS_API_KEY = os.getenv("YANDEX_JS_API_KEY", "")
YANDEX_SUGGEST_URL = "https://suggest-maps.yandex.ru/v1/suggest"
YANDEX_GEOCODER_URL = "https://geocode-maps.yandex.ru/v1/"
ADDRESS_SUGGEST_TIMEOUT = 5.0
ADDRESS_SUGGEST_MIN_INTERVAL = 0.25
_ADDRESS_SUGGEST_LAST_REQUEST: dict[str, float] = {}
_ADDRESS_SUGGEST_LAST_REQUEST_LOCK = asyncio.Lock()
# Bounding box of the bundled Rostov Oblast outline. Yandex and WFS use
# different separators for the same four coordinates.
ROSTOV_WEST, ROSTOV_SOUTH, ROSTOV_EAST, ROSTOV_NORTH = 38.22075, 45.951688, 44.319992, 50.214477
ROSTOV_SEARCH_BBOX = f"{ROSTOV_WEST},{ROSTOV_SOUTH}~{ROSTOV_EAST},{ROSTOV_NORTH}"
ROSTOV_WFS_BBOX = f"{ROSTOV_WEST},{ROSTOV_SOUTH},{ROSTOV_EAST},{ROSTOV_NORTH}"

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
WFS_PAGE_SIZE = 500
WFS_MAX_FEATURES = 10_000
PROJECT_DIR = Path(__file__).parent
GEO_PACKAGE_SRC = PROJECT_DIR.parent / "packages" / "offline_geo" / "src"
ROAD_LAYER = "skdf_open:lyr_road_by_value_line"
ROSTOV_BOUNDARY_PATH = PROJECT_DIR.parent / "static" / "rostov_boundary.geojson"
_ROSTOV_BOUNDARY_RINGS: list[list[tuple[float, float]]] | None = None


def offline_mode_enabled() -> bool:
    """Return the explicit per-process default data source mode."""
    configured = os.getenv("ALBION_OFFLINE")
    return configured is not None and configured.strip().lower() in {"1", "true", "yes", "on"}


def _geo_data_dir() -> Path:
    configured = os.getenv("ALBION_GEO_DATA_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".local/share/albion-locator"


def _geo_database_path() -> Path:
    return _geo_data_dir() / "offline_geo.sqlite3"


def _mbtiles_path() -> Path:
    configured = os.getenv("ALBION_BASEMAP_MBTILES")
    return Path(configured).expanduser() if configured else _geo_data_dir() / "basemap.mbtiles"


def _load_offline_geo_package():
    if not GEO_PACKAGE_SRC.is_dir():
        raise HTTPException(503, "Локальные геоданные недоступны: пакет offline_geo не установлен")
    package_src = str(GEO_PACKAGE_SRC)
    if package_src not in sys.path:
        sys.path.insert(0, package_src)
    try:
        return importlib.import_module("offline_geo")
    except ImportError as exc:
        raise HTTPException(503, "Локальные геоданные недоступны: пакет offline_geo не загружен") from exc


def _open_geo_index():
    package = _load_offline_geo_package()
    path = _geo_database_path()
    try:
        return package.GeoIndex(path)
    except FileNotFoundError as exc:
        raise HTTPException(503, f"Локальная база адресов и дорог не установлена: {path}") from exc
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise HTTPException(503, f"Локальная база геоданных недоступна или повреждена: {path}") from exc


def _msk61_module():
    _load_offline_geo_package()
    try:
        return importlib.import_module("offline_geo.msk61")
    except ImportError as exc:
        raise HTTPException(503, "Модуль расчёта МСК-61 недоступен") from exc


_MSK61_ERROR_TRANSLATIONS = {
    "latitude and longitude must be finite numbers":
        "Широта и долгота должны быть конечными числами.",
    "latitude or longitude is outside its geographic range":
        "Широта или долгота вне допустимого географического диапазона.",
    "latitude is outside the preliminary local domain (40 to 55 degrees)":
        "Широта вне предварительного локального домена МСК-61 (40–55°).",
    "longitude is outside the selected zone's preliminary 3-degree domain":
        "Долгота вне предварительного 3-градусного домена выбранной зоны МСК-61.",
}


def _msk61_error_message(exc: Exception) -> str:
    text = str(exc)
    return _MSK61_ERROR_TRANSLATIONS.get(text, text)
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

def _origin_allowed(origin: bytes, headers: dict[bytes, bytes]) -> bool:
    try:
        origin_text = origin.decode("latin-1")
        parsed = urlsplit(origin_text)
    except (UnicodeDecodeError, ValueError):
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False

    host = headers.get(b"x-forwarded-host") or headers.get(b"host") or b""
    host = host.decode("latin-1").split(",")[0].strip().lower()
    netloc = parsed.netloc.lower()
    if host and netloc == host:
        return True
    if netloc in {"127.0.0.1:8021", "localhost:8021"}:
        return True

    extra = os.getenv("ALBION_ALLOWED_ORIGINS", "")
    for item in extra.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            if urlsplit(item).netloc.lower() == netloc:
                return True
        except ValueError:
            continue
    return False


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
            if not _origin_allowed(origin, headers):
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

# ========== АВТОРИЗАЦИЯ ==========
PUBLIC_API_PATHS = {"/api/health", "/api/auth/login"}


def _cookie_token(scope) -> str | None:
    headers = {key.lower(): value for key, value in scope.get("headers", [])}
    raw_cookie = headers.get(b"cookie")
    if not raw_cookie:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(raw_cookie.decode("latin-1"))
    except Exception:
        return None
    morsel = cookie.get(auth.SESSION_COOKIE)
    return morsel.value if morsel else None


async def _send_json(send, status_code: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status_code,
        "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class SessionAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path.startswith("/api/") and path not in PUBLIC_API_PATHS:
            user = auth.get_user_by_token(_cookie_token(scope))
            if user is None:
                await _send_json(send, 401, {"detail": "Требуется авторизация"})
                return
            scope.setdefault("state", {})["user"] = user

        await self.app(scope, receive, send)


app.add_middleware(SessionAuthMiddleware)


def _request_user(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "Требуется авторизация")
    return user


def _require_admin(request: Request) -> dict[str, Any]:
    user = _request_user(request)
    if user.get("role") != "admin":
        raise HTTPException(403, "Недостаточно прав")
    return user

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
async def _wfs_getfeature(client, type_name, bbox_deg=None, max_features=None, cql_filter=None):
    if bbox_deg and cql_filter:
        raise ValueError("WFS bbox and CQL_FILTER are mutually exclusive")
    page_size = min(max_features or WFS_PAGE_SIZE, WFS_PAGE_SIZE)
    features: list[dict] = []
    start_index = 0

    try:
        while True:
            params = {
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeNames": type_name,
                "outputFormat": "application/json",
                "srsName": "EPSG:4326",
                "count": str(min(page_size, WFS_MAX_FEATURES - len(features))),
                "startIndex": str(start_index),
                "sortBy": "id A",
            }
            if bbox_deg:
                params["bbox"] = f"{bbox_deg[0]},{bbox_deg[1]},{bbox_deg[2]},{bbox_deg[3]},EPSG:4326"
            elif cql_filter:
                params["CQL_FILTER"] = cql_filter

            response = await client.get(WFS_BASE, params=params, timeout=WFS_TIMEOUT)
            response.raise_for_status()
            collection = response.json()
            if not isinstance(collection, dict):
                raise ValueError("Invalid WFS feature collection")
            page = collection.get("features", [])
            if not isinstance(page, list):
                raise ValueError("Invalid WFS feature list")

            returned = collection.get("numberReturned")
            if returned is not None and int(returned) != len(page):
                raise ValueError("Inconsistent WFS numberReturned")
            if not page:
                return features
            if len(features) + len(page) > WFS_MAX_FEATURES:
                raise ValueError("WFS result exceeds the local feature limit")

            # A full page that repeats a previous page means paging was ignored.
            if len(page) == page_size and any(
                page == features[offset:offset + len(page)]
                for offset in range(0, len(features), page_size)
            ):
                raise ValueError("WFS pagination did not advance")

            features.extend(page)
            start_index += len(page)

            matched = collection.get("numberMatched", collection.get("totalFeatures"))
            if matched not in (None, "unknown"):
                matched = int(matched)
                if matched < start_index:
                    raise ValueError("WFS numberMatched is smaller than returned offset")
                if start_index >= matched:
                    return features

            if len(page) < page_size:
                return features
            if len(features) >= WFS_MAX_FEATURES:
                raise ValueError("WFS result exceeds the local feature limit")
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=502,
            detail="Не удалось получить полный список дорог из сервиса СКДФ",
        ) from exc


def _load_rostov_boundary_rings() -> list[list[tuple[float, float]]]:
    """Load the outer Rostov boundary rings used for local route filtering."""
    global _ROSTOV_BOUNDARY_RINGS
    if _ROSTOV_BOUNDARY_RINGS is not None:
        return _ROSTOV_BOUNDARY_RINGS
    try:
        payload = json.loads(ROSTOV_BOUNDARY_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Invalid boundary GeoJSON")
        geometries = []
        if payload.get("type") == "FeatureCollection":
            geometries = [feature.get("geometry") for feature in payload.get("features", [])]
        elif payload.get("type") == "Feature":
            geometries = [payload.get("geometry")]
        elif payload.get("type") in {"Polygon", "MultiPolygon"}:
            geometries = [payload]
        rings: list[list[tuple[float, float]]] = []
        for geometry in geometries:
            if not isinstance(geometry, dict):
                continue
            if geometry.get("type") == "Polygon" and geometry.get("coordinates"):
                polygons = [geometry["coordinates"]]
            elif geometry.get("type") == "MultiPolygon":
                polygons = geometry.get("coordinates") or []
            else:
                continue
            for polygon in polygons:
                if not polygon or not isinstance(polygon[0], list):
                    continue
                ring = [
                    (float(point[0]), float(point[1]))
                    for point in polygon[0]
                    if isinstance(point, list) and len(point) >= 2
                ]
                if len(ring) >= 3:
                    rings.append(ring)
    except (OSError, TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(503, "Локальный контур Ростовской области недоступен") from exc
    if not rings:
        raise HTTPException(503, "В локальном файле нет контура Ростовской области")
    _ROSTOV_BOUNDARY_RINGS = rings
    return rings


def _point_in_ring(lon: float, lat: float, ring: list[tuple[float, float]]) -> bool:
    inside = False
    previous = ring[-1]
    for current in ring:
        x1, y1 = previous
        x2, y2 = current
        if (y1 > lat) != (y2 > lat):
            crossing_lon = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lon < crossing_lon:
                inside = not inside
        previous = current
    return inside


def _point_in_rostov(lon: float, lat: float) -> bool:
    return any(_point_in_ring(lon, lat, ring) for ring in _load_rostov_boundary_rings())


def _line_parts(geometry: dict | None) -> list[list[tuple[float, float]]]:
    """Return validated lon/lat line parts from a GeoJSON line geometry."""
    if not isinstance(geometry, dict):
        return []
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    raw_parts = [coordinates] if kind == "LineString" else coordinates if kind == "MultiLineString" else []
    parts: list[list[tuple[float, float]]] = []
    for raw_part in raw_parts or []:
        part: list[tuple[float, float]] = []
        for point in raw_part or []:
            try:
                lon, lat = float(point[0]), float(point[1])
            except (TypeError, ValueError, IndexError):
                continue
            if _valid_coordinates(lat, lon):
                part.append((lon, lat))
        if len(part) >= 2:
            parts.append(part)
    return parts


def _route_sections_from_features(features: list[dict], code: str) -> tuple[list[dict], str]:
    """Convert SKDF road-value features into directional sections inside Rostov."""
    sections: list[dict] = []
    names: list[str] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties") or {}
        road_name = str(properties.get("road_name") or "").strip()
        if canonical_road_code(road_name) != code:
            continue
        try:
            start_km = float(properties.get("start_km"))
            finish_km = float(properties.get("finish_km"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(start_km) and math.isfinite(finish_km)):
            continue
        parts = _line_parts(feature.get("geometry"))
        if not parts:
            continue
        points = [point for part in parts for point in part]
        if not all(_point_in_rostov(lon, lat) for lon, lat in points):
            continue
        if start_km > finish_km:
            start_km, finish_km = finish_km, start_km
            points.reverse()
        if finish_km <= start_km:
            continue
        names.append(road_name)
        sections.append({
            "start_km": round(start_km, 3),
            "finish_km": round(finish_km, 3),
            "coordinates": [[round(lon, 6), round(lat, 6)] for lon, lat in points],
        })
    sections.sort(key=lambda section: (section["start_km"], section["finish_km"]))
    name = max(names, key=len) if names else code
    return sections, name


# ========== ПРИБЛИЗИТЕЛЬНАЯ ГЕОМЕТРИЯ ДОРОГИ (OSM) ==========
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_TIMEOUT = 45.0
_OVERPASS_GEOMETRY_CACHE: dict[str, tuple[float, list[dict]]] = {}
OVERPASS_GEOMETRY_CACHE_TTL = 600.0
_HIGHWAY_RANK = {
    "motorway": 6, "trunk": 5, "primary": 4,
    "secondary": 3, "tertiary": 2, "unclassified": 1,
}


def _overpass_way_points(way: dict) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for item in way.get("geometry") or []:
        if not isinstance(item, dict):
            continue
        try:
            lon, lat = float(item.get("lon")), float(item.get("lat"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(lon) and math.isfinite(lat):
            points.append((lon, lat))
    return points


def _line_length_km(points: list[tuple[float, float]]) -> float:
    return sum(
        _haversine(points[index - 1][0], points[index - 1][1], points[index][0], points[index][1])
        for index in range(1, len(points))
    ) / 1000.0


def _way_endpoint(point: tuple[float, float]) -> tuple[float, float]:
    return (round(point[0], 6), round(point[1], 6))


def _point_distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    return _haversine(a[0], a[1], b[0], b[1]) / 1000.0


def _group_overpass_ways(ways: list[dict]) -> list[list[dict]]:
    """Group OSM ways into connected components by shared endpoints."""
    parent = list(range(len(ways)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    endpoints: list[tuple[tuple[float, float], tuple[float, float]]] = []
    by_point: dict[tuple[float, float], int] = {}
    for index, way in enumerate(ways):
        points = _overpass_way_points(way)
        first, last = _way_endpoint(points[0]), _way_endpoint(points[-1])
        endpoints.append((first, last))
        for point in (first, last):
            if point in by_point:
                union(index, by_point[point])
            else:
                by_point[point] = index

    components: dict[int, list[dict]] = {}
    for index, way in enumerate(ways):
        components.setdefault(find(index), []).append(way)
    return [component for component in components.values() if len(component) >= 1]


def _order_component_ways(ways: list[dict], hint: tuple[float, float] | None) -> list[dict]:
    """Greedily chain ways by nearest endpoints. Approximate but stable."""
    prepared = []
    for way in ways:
        points = _overpass_way_points(way)
        if len(points) >= 2:
            prepared.append((way, points))
    if not prepared:
        return []

    endpoint_counts: dict[tuple[float, float], int] = {}
    for _, points in prepared:
        for point in (_way_endpoint(points[0]), _way_endpoint(points[-1])):
            endpoint_counts[point] = endpoint_counts.get(point, 0) + 1
    dead_ends = [point for point, count in endpoint_counts.items() if count == 1]
    candidate_starts = dead_ends or list(endpoint_counts)
    if hint:
        start = min(candidate_starts, key=lambda point: _point_distance_km(point, hint))
    else:
        start = min(candidate_starts, key=lambda point: (point[1], point[0]))

    remaining = list(prepared)
    ordered: list[dict] = []
    current = start
    while remaining:
        best = None
        best_distance = math.inf
        best_reverse = False
        for way, points in remaining:
            first, last = _way_endpoint(points[0]), _way_endpoint(points[-1])
            forward = _point_distance_km(current, first)
            backward = _point_distance_km(current, last)
            if forward < best_distance:
                best, best_distance, best_reverse = (way, points), forward, False
            if backward < best_distance:
                best, best_distance, best_reverse = (way, points), backward, True
        if best is None:
            break
        way, points = best
        if best_reverse:
            points = list(reversed(points))
            ordered_way = dict(way)
            ordered_way["_ordered_points"] = points
        else:
            ordered_way = dict(way)
            ordered_way["_ordered_points"] = points
        ordered.append(ordered_way)
        current = _way_endpoint(points[-1])
        remaining = [item for item in remaining if item[0] is not way]
    return ordered


def _build_overpass_candidates(
    ways: list[dict],
    code: str,
    name_hint: str | None,
    hint: tuple[float, float] | None,
) -> list[dict]:
    candidates = []
    normalized_hint_name = normalize_text(name_hint or "")
    for component in _group_overpass_ways(ways):
        ordered = _order_component_ways(component, hint)
        if not ordered:
            continue
        names = [
            str((way.get("tags") or {}).get("name") or "").strip()
            for way in ordered
            if (way.get("tags") or {}).get("name")
        ]
        component_name = max(set(names), key=lambda value: (names.count(value), len(value))) if names else code
        highways = [
            str((way.get("tags") or {}).get("highway") or "")
            for way in ordered
        ]
        highway = max(highways, key=lambda value: _HIGHWAY_RANK.get(value, 0)) if highways else ""
        sections = []
        offset_km = 0.0
        start = end = None
        all_points: list[tuple[float, float]] = []
        for way in ordered:
            points = way.get("_ordered_points") or _overpass_way_points(way)
            if len(points) < 2:
                continue
            length_km = _line_length_km(points)
            if length_km <= 0:
                continue
            if start is None:
                start = points[0]
            end = points[-1]
            all_points.extend(points)
            sections.append({
                "start_km": round(offset_km, 3),
                "finish_km": round(offset_km + length_km, 3),
                "coordinates": [[round(lon, 6), round(lat, 6)] for lon, lat in points],
                "length_km": round(length_km, 3),
                "name": (way.get("tags") or {}).get("name") or component_name,
                "highway": (way.get("tags") or {}).get("highway") or highway,
            })
            offset_km += length_km
        if not sections:
            continue
        distance_km = 0.0
        if hint and all_points:
            distance_km = min(_point_distance_km(hint, point) for point in all_points)
        name_score = 0.0
        if normalized_hint_name:
            name_score = difflib.SequenceMatcher(
                None, normalized_hint_name, normalize_text(component_name)
            ).ratio()
        score = (
            name_score * 100.0
            + _HIGHWAY_RANK.get(highway, 0) * 8.0
            + min(offset_km, 80.0) * 0.2
            - min(distance_km, 100.0) * 1.5
        )
        candidates.append({
            "name": component_name,
            "highway": highway,
            "score": round(score, 3),
            "distance_km": round(distance_km, 3),
            "way_count": len(ordered),
            "total_length_km": round(offset_km, 3),
            "first_km": 0.0,
            "last_km": round(offset_km, 3),
            "start": [round(start[0], 6), round(start[1], 6)] if start else None,
            "end": [round(end[0], 6), round(end[1], 6)] if end else None,
            "sections": sections,
        })
    candidates.sort(key=lambda item: item["score"], reverse=True)
    return candidates[:5]


async def _fetch_overpass_ways(code: str) -> list[dict]:
    cached = _OVERPASS_GEOMETRY_CACHE.get(code)
    if cached and time.monotonic() - cached[0] < OVERPASS_GEOMETRY_CACHE_TTL:
        return cached[1]
    escaped = code.replace("\\", "\\\\").replace('"', '\\"')
    query = (
        f'[out:json][timeout:{int(OVERPASS_TIMEOUT)}];'
        f'way["ref"="{escaped}"]'
        f'({ROSTOV_SOUTH},{ROSTOV_WEST},{ROSTOV_NORTH},{ROSTOV_EAST});'
        "out tags geom;"
    )
    async with httpx.AsyncClient(headers={"User-Agent": "albion-locator/4.2.1"}) as client:
        response = await client.post(
            OVERPASS_URL, data={"data": query}, timeout=OVERPASS_TIMEOUT + 5.0
        )
        response.raise_for_status()
        payload = response.json()
    ways = []
    for element in (payload.get("elements") or []) if isinstance(payload, dict) else []:
        if not isinstance(element, dict) or element.get("type") != "way":
            continue
        points = _overpass_way_points(element)
        if len(points) >= 2:
            ways.append(element)
    if ways:
        _OVERPASS_GEOMETRY_CACHE[code] = (time.monotonic(), ways)
    return ways


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

ROAD_NAME_MATCH_THRESHOLD = 0.82
ROAD_NAME_MATCH_MARGIN = 0.12
ROAD_NAME_STOPWORDS = {
    "автомобильная", "дорога", "а", "д", "г", "город", "сл", "слобода",
    "ст", "станица", "х", "хутор", "пос", "поселок", "п", "с", "село",
}


def _road_name_tokens(value: str) -> set[str]:
    return {
        token for token in normalize_text(value).split()
        if len(token) > 1 and token not in ROAD_NAME_STOPWORDS
    }


def _explicit_road_code(value: str) -> str | None:
    """Return a recognized road-code token only when it is the whole input."""
    code = canonical_road_code(value)
    if not code:
        return None
    remainder = re.sub(r"(?<![\w])(?:[0-9]{1,3}[А-Я]-[0-9]+(?:\.[0-9]+)?|[А-Я]-[0-9]+(?:\.[0-9]+)?)(?![\w])", "", value.upper().translate(_ROAD_TRANSLATION))
    return code if not remainder.strip(" \t\r\n,.;:()[]{}«»\"'—–-") else None


def find_road_id_by_name(road_name: str) -> str | None:
    norm_input = normalize_text(road_name)
    if not norm_input:
        return None

    input_code = _explicit_road_code(road_name)
    if input_code:
        return input_code if input_code in ROADS else None

    normalized_names = {
        road_id: normalize_text(name)
        for road_id, name in ROADS.items()
    }
    exact = [road_id for road_id, name in normalized_names.items() if name == norm_input]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None

    input_tokens = _road_name_tokens(road_name)
    if len(input_tokens) < 2:
        return None

    scored: list[tuple[float, str]] = []
    for road_id, normalized_name in normalized_names.items():
        candidate_tokens = _road_name_tokens(normalized_name)
        if len(candidate_tokens) < 2 or not (input_tokens & candidate_tokens):
            continue
        score = difflib.SequenceMatcher(None, norm_input, normalized_name).ratio()
        scored.append((score, road_id))

    scored.sort(reverse=True)
    if not scored or scored[0][0] < ROAD_NAME_MATCH_THRESHOLD:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < ROAD_NAME_MATCH_MARGIN:
        return None
    return scored[0][1]

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

    value_code = canonical_road_code(value_of_the_road) if value_of_the_road else None
    name_code = canonical_road_code(road_name_from_skdf) if road_name_from_skdf else None
    has_unknown_explicit_code = any(
        code and code not in ROADS for code in (value_code, name_code)
    )

    road_id = next(
        (code for code in (value_code, name_code) if code in ROADS),
        None,
    )
    if road_id is None and not has_unknown_explicit_code:
        road_id = find_road_id_by_name(road_name_from_skdf)

    full_name = (
        f"{road_id}, {ROADS[road_id]}"
        if road_id in ROADS
        else road_name_from_skdf or "Дорога без названия"
    )
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
    use_local: bool | None = None

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
    # Задержка автоскриншота: ноль не поддерживается UI и создаёт мгновенный
    # цикл сохранения, поэтому минимальное значение — 1 секунда.
    delay: int = Field(default=3, ge=1, le=60)
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
    metadata: dict[str, Any] = Field(default_factory=dict)


class HistoryCreate(BaseModel):
    """Событие истории может быть без текстовой метки.

    В отличие от закладки имя здесь необязательно: точка с карты, геолокация
    или EXIF могут не иметь адреса/названия дороги, но событие всё равно
    должно сохраняться.
    """
    name: str = Field(default="", max_length=120)
    lat: FiniteFloat = Field(ge=-90, le=90)
    lon: FiniteFloat = Field(ge=-180, le=180)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BookmarkUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)

class MSK61ToGeographicPayload(BaseModel):
    x: FiniteFloat
    y: FiniteFloat
    zone: int = Field(ge=1, le=3)
    profile: str = Field(default="gost_51794_2008", pattern="^(calculated|gost_51794_2008)$")

class GeographicToMSK61Payload(BaseModel):
    lat: FiniteFloat = Field(ge=-90, le=90)
    lon: FiniteFloat = Field(ge=-180, le=180)
    zone: int = Field(ge=1, le=3)
    profile: str = Field(default="gost_51794_2008", pattern="^(calculated|gost_51794_2008)$")


class OnlineAddressSuggestPayload(BaseModel):
    query: str = Field(min_length=3, max_length=120)
    limit: int = Field(default=8, ge=1, le=10)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        query = value.strip()
        if len(query) < 3 or any(ord(char) < 32 for char in query):
            raise ValueError("Введите не менее 3 символов")
        return query


class ReverseGeocodePayload(BaseModel):
    lat: FiniteFloat = Field(ge=-90, le=90)
    lon: FiniteFloat = Field(ge=-180, le=180)


class OnlineAddressGeocodePayload(BaseModel):
    query: str = Field(min_length=3, max_length=320)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        query = value.strip()
        if len(query) < 3 or any(ord(char) < 32 for char in query):
            raise ValueError("Некорректный адрес")
        return query


class OnlineRoadSearchPayload(BaseModel):
    query: str = Field(min_length=2, max_length=320)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        query = value.strip()
        if len(query) < 2 or any(ord(char) < 32 for char in query):
            raise ValueError("Введите название или код дороги")
        return query


class OnlineRoadSectionsPayload(BaseModel):
    code: str = Field(min_length=2, max_length=40)

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        code = canonical_road_code(value.strip())
        if not code:
            raise ValueError("Укажите код дороги, например М-4 или Р-280")
        return code


class RoadGeometryPayload(BaseModel):
    code: str = Field(min_length=2, max_length=40)
    name: str | None = Field(default=None, max_length=240)
    lat: FiniteFloat | None = Field(default=None, ge=-90, le=90)
    lon: FiniteFloat | None = Field(default=None, ge=-180, le=180)

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        code = canonical_road_code(value.strip())
        if not code:
            raise ValueError("Укажите код дороги, например М-4 или Р-280")
        return code


class LoginPayload(BaseModel):
    login: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=200)

class CredentialsPayload(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    login: str | None = Field(default=None, max_length=120)
    password: str | None = Field(default=None, max_length=200)

class UserCreatePayload(BaseModel):
    login: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=200)
    name: str = Field(default="", max_length=120)
    role: str = Field(default="editor", pattern="^(admin|editor)$")

class UserUpdatePayload(BaseModel):
    login: str | None = Field(default=None, max_length=120)
    name: str | None = Field(default=None, max_length=120)
    role: str | None = Field(default=None, pattern="^(admin|editor)$")
    is_active: bool | None = None

class UserPasswordPayload(BaseModel):
    password: str = Field(min_length=1, max_length=200)

class StateDocument(BaseModel):
    """Персональный документ состояния (preferences/workspace).

    Размер ограничен, чтобы одна вкладка не могла раздуть БД; структура полей
    намеренно свободная — состав состояния меняется вместе с интерфейсом.
    """
    payload: dict[str, Any] = Field(default_factory=dict)
    version: int | None = Field(default=None, ge=0)


class Msk61ConvertPayload(BaseModel):
    direction: str = Field(pattern="^(to_geographic|from_geographic)$")
    zone: int = Field(ge=1, le=3)
    profile: str = Field(default="gost_51794_2008", pattern="^(calculated|gost_51794_2008)$")
    x: FiniteFloat | None = None
    y: FiniteFloat | None = None
    latitude: FiniteFloat | None = Field(default=None, ge=-90, le=90)
    longitude: FiniteFloat | None = Field(default=None, ge=-180, le=180)

# ========== НАСТРОЙКИ ==========
SETTINGS_FILE = PROJECT_DIR / "user_settings.json"

def load_settings() -> dict:
    defaults = {"delay": 3, "save_path": "screenshots", "auto_enabled": False,
                "local_boundary": True}
    if SETTINGS_FILE.exists():
        try:
            loaded = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                defaults.update(loaded)
        except (OSError, ValueError):
            pass
    defaults["save_path"] = "screenshots"
    # Общая last_point больше не используется: координаты принадлежат
    # пользователю и восстанавливаются из его workspace.
    defaults.pop("last_point", None)
    try:
        defaults["delay"] = max(1, min(60, int(defaults.get("delay", 3))))
    except (TypeError, ValueError):
        defaults["delay"] = 3
    return defaults

_SETTINGS_LOCK = asyncio.Lock()

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

@app.post("/api/auth/login")
async def auth_login(payload: LoginPayload):
    user = auth.authenticate(payload.login, payload.password)
    if user is None:
        raise HTTPException(401, "Неверный логин или пароль")

    token = auth.create_session(user["id"])
    response = JSONResponse({"status": "ok", "user": user})
    response.set_cookie(
        auth.SESSION_COOKIE,
        token,
        max_age=auth.SESSION_TTL,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response

@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    auth.delete_session(_cookie_token(request.scope))
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response

@app.get("/api/auth/me")
async def auth_me(request: Request):
    return {"user": _request_user(request)}

@app.post("/api/auth/change-credentials")
async def auth_change_credentials(payload: CredentialsPayload, request: Request):
    user = _request_user(request)
    try:
        updated = auth.change_credentials(
            user["id"],
            payload.current_password,
            payload.login,
            payload.password,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    auth.delete_user_sessions(user["id"], keep_token=_cookie_token(request.scope))
    return {"status": "ok", "user": updated}

@app.get("/api/users")
async def users_list(request: Request):
    _require_admin(request)
    return {"users": auth.list_users()}

@app.post("/api/users")
async def users_create(payload: UserCreatePayload, request: Request):
    _require_admin(request)
    try:
        user = auth.create_user(payload.login, payload.password, payload.name, payload.role)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"status": "ok", "user": user}

@app.patch("/api/users/{user_id}")
async def users_update(user_id: int, payload: UserUpdatePayload, request: Request):
    _require_admin(request)
    try:
        user = auth.update_user(
            user_id,
            login=payload.login,
            name=payload.name,
            role=payload.role,
            is_active=payload.is_active,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"status": "ok", "user": user}

@app.post("/api/users/{user_id}/password")
async def users_set_password(user_id: int, payload: UserPasswordPayload, request: Request):
    current = _require_admin(request)
    try:
        auth.set_password(user_id, payload.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    auth.delete_user_sessions(
        user_id,
        keep_token=_cookie_token(request.scope) if user_id == current["id"] else None,
    )
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
    use_local = offline_mode_enabled() if req.use_local is None else req.use_local
    if use_local:
        return await _api_locate_offline(req)

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
        road_code = None
        for value in (props.get("value_of_the_road"), rn, names.get("skdf_name")):
            candidate = canonical_road_code(str(value or ""))
            if candidate:
                road_code = candidate
                break
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
              "road_code": road_code,
              "source": "СКДФ WFS",
              "distance_m": (road_info or {}).get("_distance_m"),
              "category": cat, "coating": coat,
              "start_km": sk if road_info else None,
              "finish_km": fk if road_info else None,
              "geometry": road_info.get("geometry")} if road_info else None,
        side=side_info
    )

# ========== ЗАКЛАДКИ: ЭНДПОИНТЫ ==========
def _offline_nearest_response(lat: float, lon: float, radius: int, exclude_categories: list[int]) -> dict[str, Any]:
    with _open_geo_index() as index:
        nearest = index.nearest_road(lon, lat, max_distance_m=radius)
    async_value = nearest
    if async_value is None:
        return LocateResponse(
            point={"lat": lat, "lon": lon, "bearing": None},
            road=None,
            side=None,
        ).model_dump()
    km = nearest.get("km")
    side = {
        "side": None,
        "side_unknown": True,
        "perpendicular_m": round(nearest["distance_m"], 1),
        "distance_m": round(nearest["distance_m"], 1),
        "km_estimate": round(km, 3) if km is not None else None,
        "km_provenance": nearest.get("km_provenance", "estimated from geometry"),
        "source": nearest.get("source", "offline"),
        "projected_lon": nearest.get("snapped_lon"),
        "projected_lat": nearest.get("snapped_lat"),
    }
    geometry = nearest.get("geometry")
    if geometry is None and nearest.get("coordinates") is not None:
        geometry = {"type": "LineString", "coordinates": nearest["coordinates"]}
    road = {
        "skdf_name": nearest.get("name") or nearest.get("ref") or "Локальная дорога",
        "full_name": nearest.get("name") or nearest.get("ref") or "Локальная дорога",
        "road_code": canonical_road_code(str(nearest.get("ref") or nearest.get("name") or "")),
        "source": nearest.get("source", "локальная база"),
        "distance_m": side["distance_m"],
        "category": "",
        "coating": "",
        "start_km": nearest.get("km_start"),
        "finish_km": nearest.get("km_end"),
        "geometry": geometry,
        "source": nearest.get("source", "offline"),
        "km_provenance": side["km_provenance"],
        "category_filter_applied": not bool(exclude_categories),
    }
    return LocateResponse(
        point={"lat": lat, "lon": lon, "bearing": None},
        road=road,
        side=side,
    ).model_dump()


async def _api_locate_offline(req: LocateRequest):
    return _offline_nearest_response(req.lat, req.lon, req.radius, req.exclude_categories)


@app.get("/api/data-sources")
async def data_sources():
    db = _geo_database_path()
    tiles = _mbtiles_path()
    return {
        "default_source": "local" if offline_mode_enabled() else "external",
        "local_available": db.is_file(),
        "local_tiles_available": tiles.is_file(),
        "external_available": True,
        "address_suggest_available": bool(YANDEX_SUGGEST_API_KEY),
        "suggest_view_available": bool(YANDEX_JS_API_KEY and YANDEX_SUGGEST_API_KEY),
        "address_geocoder_available": bool(YANDEX_API_KEY),
        "fully_offline_ready": False,
        "note": "Local index and tile archive are separate generated datasets; missing local data will return an error, never fall back silently.",
    }


@app.get("/api/maps-config")
async def maps_config(request: Request):
    _request_user(request)
    response = JSONResponse({
        "suggest_view_available": bool(YANDEX_JS_API_KEY and YANDEX_SUGGEST_API_KEY),
        "geocoder_available": bool(YANDEX_API_KEY),
        "javascript_api_key": YANDEX_JS_API_KEY or None,
        "suggest_api_key": YANDEX_SUGGEST_API_KEY or None,
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/address/suggest")
async def offline_address_suggest(
    q: str = Query(default="", max_length=120),
    settlement: str | None = Query(default=None, max_length=160),
    street: str | None = Query(default=None, max_length=160),
    limit: int = Query(default=10, ge=1, le=20),
):
    with _open_geo_index() as index:
        results = index.suggest_context(q, settlement=settlement, street=street, limit=limit)
    return {"suggestions": results}


async def _allow_address_suggest(user_id: Any, action: str = "suggest") -> bool:
    now = time.monotonic()
    key = f"{action}:{user_id}"
    async with _ADDRESS_SUGGEST_LAST_REQUEST_LOCK:
        previous = _ADDRESS_SUGGEST_LAST_REQUEST.get(key)
        if previous is not None and now - previous < ADDRESS_SUGGEST_MIN_INTERVAL:
            return False
        if len(_ADDRESS_SUGGEST_LAST_REQUEST) >= 10_000:
            cutoff = now - 60.0
            for old_key, timestamp in list(_ADDRESS_SUGGEST_LAST_REQUEST.items()):
                if timestamp < cutoff:
                    _ADDRESS_SUGGEST_LAST_REQUEST.pop(old_key, None)
            if len(_ADDRESS_SUGGEST_LAST_REQUEST) >= 10_000:
                return False
        _ADDRESS_SUGGEST_LAST_REQUEST[key] = now
    return True


def _yandex_suggest_text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("text")
    return value.strip() if isinstance(value, str) else ""


def _normalize_yandex_suggestions(payload: Any, limit: int) -> list[dict[str, str]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise HTTPException(502, "Сервис адресных подсказок вернул некорректный ответ")
    suggestions: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in payload["results"]:
        if not isinstance(item, dict):
            continue
        title = _yandex_suggest_text(item.get("title"))
        subtitle = _yandex_suggest_text(item.get("subtitle"))
        display_name = _yandex_suggest_text(item.get("displayText")) or ", ".join(
            part for part in (title, subtitle) if part
        )
        if not display_name or display_name.casefold() in seen:
            continue
        seen.add(display_name.casefold())
        uri = item.get("uri")
        suggestion_id = uri.strip() if isinstance(uri, str) and uri.strip() else display_name
        suggestions.append({
            "id": suggestion_id[:240],
            "title": title or display_name,
            "subtitle": subtitle,
            "display_name": display_name[:320],
            "query": display_name[:320],
        })
        if len(suggestions) >= limit:
            break
    return suggestions


async def _fetch_yandex_suggestions(query: str, limit: int) -> list[dict[str, str]]:
    if not YANDEX_SUGGEST_API_KEY:
        raise HTTPException(503, "Не настроен YANDEX_SUGGEST_API_KEY для онлайн-подсказок адреса")
    params = {
        "text": query,
        "apikey": YANDEX_SUGGEST_API_KEY,
        "lang": "ru_RU",
        "results": limit,
        "bbox": ROSTOV_SEARCH_BBOX,
        "attrs": "uri",
        "print_address": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=ADDRESS_SUGGEST_TIMEOUT) as client:
            response = await client.get(YANDEX_SUGGEST_URL, params=params)
        if response.status_code == 429:
            raise HTTPException(429, "Превышен лимит онлайн-подсказок Яндекса; повторите позже")
        if response.status_code in {401, 403}:
            raise HTTPException(503, "Ключ Яндекс Geosuggest не имеет доступа к API подсказок")
        response.raise_for_status()
        payload = response.json()
    except HTTPException:
        raise
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "Сервис онлайн-подсказок Яндекса не ответил вовремя") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Не удалось связаться с сервисом онлайн-подсказок Яндекса") from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(502, "Сервис адресных подсказок вернул некорректный ответ") from exc
    return _normalize_yandex_suggestions(payload, limit)


@app.post("/api/address/online/suggest")
async def online_address_suggest(payload: OnlineAddressSuggestPayload, request: Request):
    user = _request_user(request)
    if not await _allow_address_suggest(user.get("id", "unknown")):
        raise HTTPException(429, "Слишком частые запросы подсказок; подождите немного")
    suggestions = await _fetch_yandex_suggestions(payload.query, payload.limit)
    return {"suggestions": suggestions, "source": "Yandex Geosuggest"}


def _component_by_kinds(metadata: dict, kinds: set[str]) -> str:
    for component in ((metadata.get("Address") or {}).get("Components") or []):
        if isinstance(component, dict) and component.get("kind") in kinds:
            name = str(component.get("name") or "").strip()
            if name:
                return name
    return ""


@app.post("/api/geocode/reverse")
async def reverse_geocode(payload: ReverseGeocodePayload, request: Request):
    """Координаты -> адрес и район. Возвращает null-поля, если Яндекса нет."""
    user = _request_user(request)
    if not await _allow_address_suggest(user.get("id", "unknown"), "reverse"):
        raise HTTPException(429, "Слишком частые запросы адреса; подождите немного")
    if not YANDEX_API_KEY:
        return {"address": None, "district": None, "source": None,
                "detail": "YANDEX_API_KEY не настроен"}
    params_base = {
        "apikey": YANDEX_API_KEY,
        "geocode": f"{payload.lon},{payload.lat}",
        "format": "json",
        "lang": "ru_RU",
        "results": 1,
        "sco": "longlat",
    }
    members: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(timeout=ADDRESS_SUGGEST_TIMEOUT) as client:
            # Сначала ближайший дом: он даёт улицу и номер. Если дома рядом нет
            # (лес, поле, трасса), повторяем без kind и получаем более грубый адрес.
            for extra in ({"kind": "house"}, {}):
                response = await client.get(YANDEX_GEOCODER_URL, params={**params_base, **extra})
                if response.status_code == 429:
                    raise HTTPException(429, "Превышен лимит геокодирования Яндекса; повторите позже")
                if response.status_code in {401, 403}:
                    raise HTTPException(503, "Ключ Яндекс Геокодера не имеет доступа к этому API")
                response.raise_for_status()
                members = response.json()["response"]["GeoObjectCollection"]["featureMember"]
                if members:
                    break
    except HTTPException:
        raise
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "Сервис геокодирования Яндекса не ответил вовремя") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Не удалось связаться с сервисом геокодирования Яндекса") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(502, "Сервис геокодирования Яндекса вернул некорректный ответ") from exc
    if not members:
        return {"address": None, "district": None, "source": "Yandex Geocoder"}
    try:
        geo_object = members[0]["GeoObject"]
        metadata = geo_object["metaDataProperty"]["GeocoderMetaData"]
        address = str(metadata.get("text") or "").strip() or None
        # Район города/области: Яндекс помечает его kind=district (реже area).
        district = _component_by_kinds(metadata, {"district", "area"}) or None
    except (KeyError, TypeError) as exc:
        raise HTTPException(502, "Не удалось разобрать адрес выбранной точки") from exc
    return {"address": address, "district": district, "source": "Yandex Geocoder"}


@app.post("/api/address/online/geocode")
async def online_address_geocode(payload: OnlineAddressGeocodePayload, request: Request):
    user = _request_user(request)
    if not await _allow_address_suggest(user.get("id", "unknown"), "geocode"):
        raise HTTPException(429, "Слишком частые запросы адреса; подождите немного")
    if not YANDEX_API_KEY:
        raise HTTPException(503, "Не настроен YANDEX_API_KEY для определения координат адреса")
    geocode_query = payload.query
    normalized_query = geocode_query.casefold().replace("ё", "е")
    if "ростовская область" not in normalized_query:
        geocode_query += ", Ростовская область"
    if "россия" not in normalized_query:
        geocode_query += ", Россия"
    params = {
        "apikey": YANDEX_API_KEY,
        "geocode": geocode_query,
        "format": "json",
        "lang": "ru_RU",
        "results": 1,
        "bbox": ROSTOV_SEARCH_BBOX,
        "rspn": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=ADDRESS_SUGGEST_TIMEOUT) as client:
            response = await client.get(YANDEX_GEOCODER_URL, params=params)
        if response.status_code == 429:
            raise HTTPException(429, "Превышен лимит геокодирования Яндекса; повторите позже")
        if response.status_code in {401, 403}:
            raise HTTPException(503, "Ключ Яндекс Геокодера не имеет доступа к этому API")
        response.raise_for_status()
        data = response.json()
        members = data["response"]["GeoObjectCollection"]["featureMember"]
    except HTTPException:
        raise
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "Сервис геокодирования Яндекса не ответил вовремя") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Не удалось связаться с сервисом геокодирования Яндекса") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(502, "Сервис геокодирования Яндекса вернул некорректный ответ") from exc
    if not members:
        raise HTTPException(404, "Для выбранной подсказки координаты не найдены")
    try:
        geo_object = members[0]["GeoObject"]
        lon, lat = map(float, geo_object["Point"]["pos"].split())
        metadata = geo_object["metaDataProperty"]["GeocoderMetaData"]
        formatted_address = str(metadata.get("text") or payload.query)[:320]
        precision = str(metadata.get("precision") or "unknown")[:40]
        components = metadata.get("Address", {}).get("Components", [])
        in_rostov_oblast = any(
            isinstance(component, dict)
            and component.get("kind") in {"province", "area"}
            and str(component.get("name", "")).strip().casefold().replace("ё", "е") == "ростовская область"
            for component in components
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(502, "Не удалось определить координаты выбранного адреса") from exc
    if not (math.isfinite(lat) and math.isfinite(lon)
            and ROSTOV_SOUTH <= lat <= ROSTOV_NORTH
            and ROSTOV_WEST <= lon <= ROSTOV_EAST
            and in_rostov_oblast):
        raise HTTPException(404, "Адрес не найден в Ростовской области; уточните адрес и регион")
    return {
        "lat": lat,
        "lon": lon,
        "address": formatted_address,
        "precision": precision,
        "source": "Yandex Geocoder",
    }


_ROAD_NAME_RE = re.compile(
    r"\b(?:дорога|трасса|шоссе|автодорога|магистраль|тракт|обход|объездная|подъезд|развязка)\b",
    re.IGNORECASE,
)


def _component_name(metadata: dict, kind: str) -> str:
    for component in ((metadata.get("Address") or {}).get("Components") or []):
        if isinstance(component, dict) and component.get("kind") == kind:
            return str(component.get("name") or "").strip()
    return ""


def _looks_like_road_name(metadata: dict, text: str) -> bool:
    """Accept named roads/highways, not ordinary city streets."""
    street_name = _component_name(metadata, "street")
    return bool(_ROAD_NAME_RE.search(street_name or text))


def _road_geocode_queries(query: str) -> list[str]:
    """Build conservative Yandex Geocoder queries for a road or highway name."""
    cleaned = " ".join(query.split())[:320]
    code = canonical_road_code(cleaned)
    if not code:
        street = re.search(
            r"(?:улица|ул\.?|проспект|пр-?кт|шоссе|переулок|проезд|набережная|бульвар|площадь)\s+[^,;]+",
            cleaned,
            re.IGNORECASE,
        )
        base = street.group(0) if street else cleaned
        return [base, f"{base}, Ростовская область"]
    # Bare route codes resolve to the route object; adding the region name as
    # ordinary query text can instead match unrelated localities/houses.
    return [code, f"{code}, Ростовская область"]


@app.post("/api/roads/online/search")
async def online_roads_search(payload: OnlineRoadSearchPayload, request: Request):
    user = _request_user(request)
    if not await _allow_address_suggest(user.get("id", "unknown"), "road-search"):
        raise HTTPException(429, "Слишком частые запросы дорог; подождите немного")
    if not YANDEX_API_KEY:
        raise HTTPException(503, "Не настроен YANDEX_API_KEY для поиска дорог")
    code = canonical_road_code(payload.query)
    params_base = {
        "apikey": YANDEX_API_KEY,
        "format": "json",
        "lang": "ru_RU",
        "results": 10,
        "bbox": ROSTOV_SEARCH_BBOX,
        "rspn": 1,
        # The Geocoder has no separate highway kind; linear road objects,
        # including М-4/Р-280, are returned as streets. Nothing else is accepted.
        "kind": "street",
    }
    matches: list[dict[str, Any]] = []
    seen: set[tuple[str, float, float]] = set()
    try:
        async with httpx.AsyncClient(timeout=ADDRESS_SUGGEST_TIMEOUT) as client:
            for query in _road_geocode_queries(payload.query):
                response = await client.get(YANDEX_GEOCODER_URL, params={**params_base, "geocode": query})
                if response.status_code == 429:
                    raise HTTPException(429, "Превышен лимит геокодирования Яндекса; повторите позже")
                if response.status_code in {401, 403}:
                    raise HTTPException(503, "Ключ Яндекс Геокодера не имеет доступа к этому API")
                response.raise_for_status()
                members = response.json().get("response", {}).get("GeoObjectCollection", {}).get("featureMember", [])
                for member in members:
                    if not isinstance(member, dict):
                        continue
                    geo_object = member.get("GeoObject") or {}
                    metadata = (geo_object.get("metaDataProperty") or {}).get("GeocoderMetaData") or {}
                    try:
                        lon, lat = map(float, (geo_object.get("Point") or {}).get("pos", "").split())
                    except (TypeError, ValueError):
                        continue
                    components = (metadata.get("Address") or {}).get("Components") or []
                    in_rostov = any(
                        isinstance(component, dict)
                        and component.get("kind") in {"province", "area"}
                        and str(component.get("name", "")).strip().casefold().replace("ё", "е") == "ростовская область"
                        for component in components
                    )
                    if not (in_rostov and math.isfinite(lat) and math.isfinite(lon)
                            and ROSTOV_SOUTH <= lat <= ROSTOV_NORTH
                            and ROSTOV_WEST <= lon <= ROSTOV_EAST):
                        continue
                    if metadata.get("kind") != "street":
                        continue
                    text = str(metadata.get("text") or query)[:320]
                    if code:
                        if canonical_road_code(text) != code:
                            continue
                    elif not _looks_like_road_name(metadata, text):
                        continue
                    if code and matches:
                        # Route codes resolve to many kilometre points; one road
                        # result is enough because the km slider selects the point.
                        continue
                    if any(item["title"].casefold() == text.casefold() for item in matches):
                        continue
                    key = (text.casefold(), round(lat, 6), round(lon, 6))
                    if key in seen:
                        continue
                    seen.add(key)
                    matches.append({
                        "title": text,
                        "lat": lat,
                        "lon": lon,
                        "kind": "road",
                        "yandex_kind": str(metadata.get("kind") or "unknown"),
                        "precision": str(metadata.get("precision") or "unknown"),
                        "road_code": code,
                        "source": "Yandex Geocoder",
                    })
                if matches:
                    break
    except HTTPException:
        raise
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "Сервис поиска дорог Яндекса не ответил вовремя") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Не удалось связаться с сервисом поиска дорог Яндекса") from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(502, "Сервис поиска дорог Яндекса вернул некорректный ответ") from exc
    if not matches:
        raise HTTPException(404, "Дорога не найдена в Ростовской области; уточните название или код")
    return {"roads": matches[:10], "source": "Yandex Geocoder"}


@app.post("/api/roads/online/sections")
async def online_road_sections(payload: OnlineRoadSectionsPayload, request: Request):
    """Return official SKDF chainage sections of a road inside Rostov Oblast."""
    user = _request_user(request)
    if not await _allow_address_suggest(user.get("id", "unknown"), "road-sections"):
        raise HTTPException(429, "Слишком частые запросы участков дороги; подождите немного")
    code = payload.code
    escaped_code = code.replace("'", "''")
    cql_filter = (
        f"BBOX(geom,{ROSTOV_WFS_BBOX},'EPSG:4326') "
        f"AND road_name LIKE '{escaped_code}%'"
    )
    try:
        async with httpx.AsyncClient(headers={"User-Agent": "albion/4.2.1"}) as client:
            features = await _wfs_getfeature(
                client,
                ROAD_LAYER,
                max_features=5_000,
                cql_filter=cql_filter,
            )
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise HTTPException(502, "Сервис СКДФ вернул некорректный список участков дороги") from exc

    sections, name = _route_sections_from_features(features, code)
    if not sections:
        raise HTTPException(
            404,
            f"Для дороги {code} в СКДФ нет участков с официальным километражом "
            "в Ростовской области",
        )
    first_km = sections[0]["start_km"]
    last_km = max(section["finish_km"] for section in sections)
    return {
        "code": code,
        "name": name,
        "first_km": first_km,
        "last_km": last_km,
        "total_length_km": round(last_km - first_km, 3),
        "sections": sections,
        "source": "СКДФ WFS: lyr_road_by_value_line",
        "provenance": (
            "Официальные start_km/finish_km СКДФ; участки вне контура Ростовской "
            "области исключены. Точка внутри участка интерполируется по его геометрии."
        ),
    }


@app.post("/api/roads/overpass/geometry")
async def overpass_road_geometry(payload: RoadGeometryPayload, request: Request):
    """Approximate road axis from OSM when SKDF has no official chainage."""
    user = _request_user(request)
    if not await _allow_address_suggest(user.get("id", "unknown"), "road-geometry"):
        raise HTTPException(429, "Слишком частые запросы геометрии дорог; подождите немного")
    try:
        ways = await _fetch_overpass_ways(payload.code)
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise HTTPException(502, "Overpass API недоступен или вернул некорректные данные") from exc
    if not ways:
        raise HTTPException(404, f"В OpenStreetMap не найдена геометрия для {payload.code}")
    hint = None
    if payload.lat is not None and payload.lon is not None:
        hint = (payload.lon, payload.lat)
    candidates = _build_overpass_candidates(ways, payload.code, payload.name, hint)
    if not candidates:
        raise HTTPException(404, f"Не удалось собрать ось дороги {payload.code} из OSM")
    return {
        "code": payload.code,
        "source": "OpenStreetMap Overpass",
        "approximate": True,
        "provenance": (
            "Приблизительная ось по OSM: официальный километраж СКДФ отсутствует, "
            "километры относительные (0 — начало выбранного направления). "
            "Геометрия OSM может быть неполной и не совпадать с официальной осью."
        ),
        "candidates": candidates,
    }


@app.get("/api/roads/search")
async def offline_roads_search(
    q: str | None = Query(default=None, max_length=160),
    ref: str | None = Query(default=None, max_length=80),
    km: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=10, ge=1, le=20),
):
    try:
        with _open_geo_index() as index:
            results = index.search_roads(q, ref=ref, km=km, limit=limit)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"roads": results}


@app.post("/api/msk61/to-geographic")
async def msk61_to_geographic(payload: MSK61ToGeographicPayload):
    module = _msk61_module()
    try:
        result = module.msk61_to_latlon(payload.x, payload.y, zone=payload.zone, profile=payload.profile)
    except ValueError as exc:
        raise HTTPException(422, _msk61_error_message(exc)) from exc
    return {
        "lat": result.latitude,
        "lon": result.longitude,
        "zone": result.zone,
        "profile": result.profile.value,
        "status": result.status,
        "warning": result.warning,
        "requires_confirmation": True,
        "can_place_on_map": result.profile.value == "gost_51794_2008",
    }


@app.post("/api/msk61/from-geographic")
async def geographic_to_msk61(payload: GeographicToMSK61Payload):
    module = _msk61_module()
    try:
        result = module.latlon_to_msk61(payload.lat, payload.lon, zone=payload.zone, profile=payload.profile)
    except ValueError as exc:
        raise HTTPException(422, _msk61_error_message(exc)) from exc
    return {
        "x": result.x,
        "y": result.y,
        "zone": result.zone,
        "profile": result.profile.value,
        "status": result.status,
        "warning": result.warning,
    }


@app.get("/tiles/{z}/{x}/{y}.png", include_in_schema=False)
async def offline_raster_tile(z: int, x: int, y: int):
    if not (0 <= z <= 20 and 0 <= x < (1 << z) and 0 <= y < (1 << z)):
        raise HTTPException(404, "tile not found")
    path = _mbtiles_path()
    if not path.is_file():
        raise HTTPException(503, "Offline tile archive is not installed")
    try:
        with sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True) as connection:
            row = connection.execute(
                "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                (z, x, (1 << z) - 1 - y),
            ).fetchone()
    except sqlite3.Error as exc:
        raise HTTPException(503, "Offline tile archive is unreadable") from exc
    if row is None:
        raise HTTPException(404, "tile not found")
    return Response(content=row[0], media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


MAX_STATE_PAYLOAD_BYTES = 256 * 1024


def _state_payload_too_large(payload: dict[str, Any]) -> bool:
    try:
        encoded = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return True
    return len(encoded.encode("utf-8")) > MAX_STATE_PAYLOAD_BYTES


@app.get("/api/user/preferences")
async def get_user_preferences(request: Request):
    user = _request_user(request)
    return auth.get_preferences(user["id"]) or {"payload": {}, "version": 0}


@app.put("/api/user/preferences")
async def put_user_preferences(payload: StateDocument, request: Request):
    user = _request_user(request)
    if _state_payload_too_large(payload.payload):
        raise HTTPException(413, "Состояние настроек слишком большое")
    try:
        return auth.save_preferences(user["id"], payload.payload, payload.version)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.delete("/api/user/preferences")
async def delete_user_preferences(request: Request):
    user = _request_user(request)
    auth.clear_preferences(user["id"])
    return {"status": "ok"}


@app.get("/api/user/workspace")
async def get_user_workspace(request: Request):
    user = _request_user(request)
    return auth.get_workspace(user["id"]) or {"payload": {}, "version": 0}


@app.put("/api/user/workspace")
async def put_user_workspace(payload: StateDocument, request: Request):
    user = _request_user(request)
    if _state_payload_too_large(payload.payload):
        raise HTTPException(413, "Состояние рабочей сессии слишком большое")
    try:
        return auth.save_workspace(user["id"], payload.payload, payload.version)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.delete("/api/user/workspace")
async def delete_user_workspace(request: Request):
    user = _request_user(request)
    auth.clear_workspace(user["id"])
    return {"status": "ok"}


@app.get("/api/bookmarks")
async def get_bookmarks(request: Request):
    user = _request_user(request)
    return {"bookmarks": auth.list_bookmarks(user["id"])}

@app.post("/api/bookmarks")
async def add_bookmark(payload: BookmarkCreate, request: Request):
    user = _request_user(request)
    bookmark = auth.add_bookmark(
        user["id"],
        payload.name.strip() or "Без имени",
        payload.lat,
        payload.lon,
        payload.metadata,
    )
    return {"status": "ok", "bookmark": bookmark}

@app.patch("/api/bookmarks/{bookmark_id}")
async def update_bookmark(bookmark_id: str, payload: BookmarkUpdate, request: Request):
    user = _request_user(request)
    try:
        bookmark = auth.update_bookmark(user["id"], bookmark_id, payload.name.strip())
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"status": "ok", "bookmark": bookmark}

@app.delete("/api/bookmarks/{bookmark_id}")
async def delete_bookmark(bookmark_id: str, request: Request):
    user = _request_user(request)
    auth.delete_bookmark(user["id"], bookmark_id)
    return {"status": "ok"}


# ========== ИСТОРИЯ ТОЧЕК ==========
@app.get("/api/history")
async def get_history(request: Request, limit: int = Query(default=20, ge=1, le=100)):
    user = _request_user(request)
    return {"entries": auth.list_history(user["id"], limit)}


@app.post("/api/history")
async def push_history(payload: HistoryCreate, request: Request):
    user = _request_user(request)
    auth.push_history(user["id"], payload.lat, payload.lon,
                      payload.name or "", payload.metadata.get("source", "manual"))
    return {"status": "ok"}


@app.delete("/api/history")
async def clear_history(request: Request):
    user = _request_user(request)
    auth.delete_history(user["id"])
    return {"status": "ok"}


@app.delete("/api/history/{entry_id}")
async def delete_history_entry(entry_id: int, request: Request):
    user = _request_user(request)
    auth.delete_history(user["id"], entry_id)
    return {"status": "ok"}


# ========== СТАТИКА ==========
_STATIC_DIR = PROJECT_DIR.parent / "static"

@app.get("/static/rostov_boundary.geojson")
async def boundary_geojson():
    return FileResponse(_STATIC_DIR / "rostov_boundary.geojson", media_type="application/geo+json")


# Клиентские модули отдаются по явному allowlist: каталог static не публикуется
# целиком, чтобы рядом лежащие файлы не становились доступны по прямой ссылке.
_STATIC_JS_FILES = {"geo-core.js"}


@app.get("/static/js/{filename}")
async def static_js(filename: str):
    if filename not in _STATIC_JS_FILES:
        raise HTTPException(404, "not found")
    path = _STATIC_DIR / "js" / filename
    if not path.is_file():
        raise HTTPException(404, "not found")
    response = FileResponse(path, media_type="application/javascript")
    response.headers["Cache-Control"] = "no-cache"
    return response


# Vendored browser libraries: приложение не зависит от CDN и продолжает
# работать при блокировке внешних скриптов (Opera blocker/offline LAN).
_STATIC_VENDOR_FILES = {
    "leaflet.js",
    "leaflet.css",
    "dom-to-image-more.min.js",
    "images/marker-icon.png",
    "images/marker-icon-2x.png",
    "images/marker-shadow.png",
    "images/layers.png",
    "images/layers-2x.png",
}
_VENDOR_MEDIA_TYPES = {
    ".js": "application/javascript",
    ".css": "text/css",
    ".png": "image/png",
}


@app.get("/static/vendor/{filename:path}")
async def static_vendor(filename: str):
    if filename not in _STATIC_VENDOR_FILES:
        raise HTTPException(404, "not found")
    vendor_dir = _STATIC_DIR / "vendor"
    path = (vendor_dir / filename).resolve()
    if vendor_dir.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "not found")
    response = FileResponse(
        path,
        media_type=_VENDOR_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
    )
    response.headers["Cache-Control"] = "no-cache"
    return response

@app.get("/", include_in_schema=False)
async def root():
    response = FileResponse(_STATIC_DIR / "index.html")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["X-Frame-Options"] = "DENY"
    return response

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8021)
