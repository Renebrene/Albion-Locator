"""Parse city/address/side/coordinates from XLS/XLSX tables.

The column detection follows the address tables used by the company site:
find a header row containing city/address/coordinates, then read data rows
below it. Only the fields needed by the locator map are kept.
"""
from __future__ import annotations

import io
import math
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

MAX_ROWS = 20_000
MAX_SHEETS = 20
MAX_COLUMNS = 120
HEADER_SCAN_ROWS = 100
MAX_ERRORS = 50

CITY_HEADERS = {
    "город", "город1", "город 1", "город2", "город 2",
    "населенный пункт", "населенный пункт 1",
}
ADDRESS_HEADERS = {
    "адрес", "адресс", "адрес размещения", "адрес объекта",
    "местоположение", "адрес установки",
}
SIDE_HEADERS = {"сторона", "сторона конструкции", "направление", "side"}
COORD_HEADERS = {
    "координаты", "координаты wgs84", "координаты wgs 84",
    "координаты wgs-84", "координаты объекта", "coords", "coord",
}
LAT_HEADERS = {"широта", "latitude", "lat", "широта wgs84"}
LON_HEADERS = {"долгота", "longitude", "lon", "lng", "долгота wgs84"}

_NUMBER_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
_SPACES_RE = re.compile(r"\s+")


class ExcelImportError(ValueError):
    """The uploaded workbook cannot be parsed as a supported table."""


class ExcelDependencyError(RuntimeError):
    """The optional XLS/XLSX reader is not installed."""


@dataclass
class ParsedWorkbook:
    points: list[dict[str, Any]] = field(default_factory=list)
    skipped: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    sheets: list[dict[str, Any]] = field(default_factory=list)


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return _SPACES_RE.sub(" ", str(value).replace("\xa0", " ")).strip()


def normalize_header(value: Any) -> str:
    text = normalize_text(value).lower().replace("ё", "е")
    text = re.sub(r"[^\w\s.-]", " ", text)
    return _SPACES_RE.sub(" ", text).strip()


def _number(value: Any) -> float | None:
    text = normalize_text(value)
    if not text:
        return None
    text = text.replace(" ", "").replace(",", ".")
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _coordinate_pair(value: Any) -> tuple[float, float] | None:
    text = normalize_text(value)
    if not text:
        return None
    numbers = _NUMBER_RE.findall(text)
    parsed: list[float] = []
    if len(numbers) == 2:
        parsed = [float(number.replace(",", ".")) for number in numbers]
    elif len(numbers) in {3, 4} and all("." not in number for number in numbers[:2]):
        # Decimal commas: "47,237201, 39,816711" -> 47.237201, 39.816711.
        first = float(numbers[0] + "." + numbers[1])
        if len(numbers) == 3:
            parsed = [first, float(numbers[2].replace(",", "."))]
        else:
            parsed = [first, float(numbers[2] + "." + numbers[3])]
    if len(parsed) != 2 or not all(math.isfinite(item) for item in parsed):
        return None
    latitude, longitude = parsed
    if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
        return None
    return latitude, longitude


def normalize_side(value: Any) -> str:
    text = normalize_text(value)
    if not text:
        return ""
    mapping = {"А": "A", "Б": "B", "С": "C"}
    return mapping.get(text.upper(), text)


def _header_kind(value: Any) -> str | None:
    norm = normalize_header(value)
    if not norm:
        return None
    if norm in LAT_HEADERS:
        return "lat"
    if norm in LON_HEADERS:
        return "lon"
    if norm in COORD_HEADERS or "координат" in norm:
        return "coords"
    if norm in CITY_HEADERS or norm.startswith("город"):
        return "city"
    if norm in ADDRESS_HEADERS or (
        norm.startswith("адрес") and "программ" not in norm
    ):
        return "address"
    if norm in SIDE_HEADERS or ("сторон" in norm and "тип" not in norm):
        return "side"
    return None


def _map_headers(row: Iterable[Any]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, value in enumerate(row):
        kind = _header_kind(value)
        if kind and kind not in mapping:
            mapping[kind] = index
    return mapping


def _header_usable(mapping: dict[str, int]) -> bool:
    has_location = "address" in mapping or "city" in mapping
    has_coordinates = "coords" in mapping or (
        "lat" in mapping and "lon" in mapping
    )
    return has_location and has_coordinates


def _cell(row: list[Any], index: int | None) -> Any:
    if index is None or index >= len(row):
        return None
    return row[index]


def _read_xlsx(data: bytes) -> list[tuple[str, list[list[Any]]]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ExcelDependencyError("Установите openpyxl для чтения XLSX") from exc
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ExcelImportError(f"Не удалось открыть XLSX: {exc}") from exc
    sheets = []
    try:
        for worksheet in workbook.worksheets[:MAX_SHEETS]:
            rows = []
            for row_index, row in enumerate(
                worksheet.iter_rows(values_only=True), start=1
            ):
                if row_index > MAX_ROWS:
                    break
                rows.append(list(row[:MAX_COLUMNS]))
            sheets.append((worksheet.title, rows))
    finally:
        workbook.close()
    return sheets


def _read_xls(data: bytes) -> list[tuple[str, list[list[Any]]]]:
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ExcelDependencyError("Установите xlrd для чтения XLS") from exc
    try:
        workbook = xlrd.open_workbook(file_contents=data, on_demand=True)
    except Exception as exc:
        raise ExcelImportError(f"Не удалось открыть XLS: {exc}") from exc
    sheets = []
    try:
        for name in workbook.sheet_names()[:MAX_SHEETS]:
            worksheet = workbook.sheet_by_name(name)
            rows = [
                worksheet.row_values(index)[:MAX_COLUMNS]
                for index in range(min(worksheet.nrows, MAX_ROWS))
            ]
            sheets.append((name, rows))
    finally:
        workbook.release_resources()
    return sheets


def _read_sheets(data: bytes) -> list[tuple[str, list[list[Any]]]]:
    if not data:
        raise ExcelImportError("Файл пуст")
    if zipfile.is_zipfile(io.BytesIO(data)):
        return _read_xlsx(data)
    return _read_xls(data)


def parse_workbook(data: bytes, filename: str = "") -> ParsedWorkbook:
    """Parse the first usable table on every sheet of an XLS/XLSX file."""
    suffix = Path(filename).suffix.lower()
    if suffix not in {".xls", ".xlsx", ".xlsm"}:
        raise ExcelImportError("Поддерживаются только файлы .xls и .xlsx")
    result = ParsedWorkbook()
    seen: set[tuple[float, float, str, str]] = set()

    for sheet_name, rows in _read_sheets(data):
        header_index = None
        mapping: dict[str, int] = {}
        for index, row in enumerate(rows[:HEADER_SCAN_ROWS]):
            candidate = _map_headers(row)
            if _header_usable(candidate):
                header_index = index
                mapping = candidate
                break
        if header_index is None:
            result.sheets.append({"name": sheet_name, "rows": 0, "skipped": True})
            continue

        parsed_rows = 0
        for row_index in range(header_index + 1, len(rows)):
            row = rows[row_index]
            if not any(normalize_text(value) for value in row):
                continue
            city = normalize_text(_cell(row, mapping.get("city")))
            address = normalize_text(_cell(row, mapping.get("address")))
            side = normalize_side(_cell(row, mapping.get("side")))
            coordinates = None
            if "lat" in mapping and "lon" in mapping:
                latitude = _number(_cell(row, mapping["lat"]))
                longitude = _number(_cell(row, mapping["lon"]))
                if latitude is not None and longitude is not None:
                    coordinates = (latitude, longitude)
            coords_cell = _cell(row, mapping.get("coords"))
            if coordinates is None and "coords" in mapping:
                coordinates = _coordinate_pair(coords_cell)
            if not city and not address:
                result.skipped += 1
                continue
            if coordinates is None:
                if len(result.errors) < MAX_ERRORS and normalize_text(coords_cell):
                    result.errors.append({
                        "sheet": sheet_name,
                        "row": row_index + 1,
                        "reason": "не удалось разобрать координаты",
                    })
                result.skipped += 1
                continue
            latitude, longitude = coordinates
            if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
                if len(result.errors) < MAX_ERRORS:
                    result.errors.append({
                        "sheet": sheet_name,
                        "row": row_index + 1,
                        "reason": "координаты вне допустимого диапазона",
                    })
                result.skipped += 1
                continue
            latitude = round(latitude, 6)
            longitude = round(longitude, 6)
            key = (latitude, longitude, address.casefold(), side.casefold())
            if key in seen:
                result.skipped += 1
                continue
            seen.add(key)
            result.points.append({
                "city": city,
                "address": address,
                "side": side,
                "lat": latitude,
                "lon": longitude,
                "sheet": sheet_name,
                "row": row_index + 1,
            })
            parsed_rows += 1
            if len(result.points) >= MAX_ROWS:
                break
        result.sheets.append({"name": sheet_name, "rows": parsed_rows, "skipped": False})

    if not result.points:
        detail = "В файле не найдены строки с адресом/городом и координатами."
        raise ExcelImportError(detail)
    return result
