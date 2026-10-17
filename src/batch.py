"""
Пакетная обработка фото: извлечение GPS из множества файлов
"""
from fastapi import APIRouter, UploadFile, File, HTTPException
from typing import List
import io
import math
from PIL import Image
import piexif
from fractions import Fraction

router = APIRouter()
Image.MAX_IMAGE_PIXELS = 40_000_000

MAX_FILE_BYTES = 12 * 1024 * 1024
MAX_BATCH_FILES = 50
MAX_BATCH_BYTES = 100 * 1024 * 1024

def _dms_to_dd(dms: tuple, ref: bytes) -> float:
    if ref not in (b"N", b"S", b"E", b"W") or len(dms) != 3:
        raise ValueError("Invalid GPS coordinate reference")
    degrees, minutes, seconds = (float(Fraction(value[0], value[1])) for value in dms)
    max_degrees = 90 if ref in (b"N", b"S") else 180
    if (not all(math.isfinite(value) for value in (degrees, minutes, seconds))
            or degrees < 0 or minutes < 0 or minutes >= 60
            or seconds < 0 or seconds >= 60 or degrees > max_degrees
            or (degrees == max_degrees and (minutes != 0 or seconds != 0))):
        raise ValueError("Invalid GPS coordinate range")
    result = degrees + minutes / 60 + seconds / 3600
    return -result if ref in (b"S", b"W") else result

def extract_gps_safe(data: bytes) -> dict | None:
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.format not in {"JPEG", "TIFF"} or img.width * img.height > 40_000_000:
                return None
            ex = piexif.load(img.info.get("exif", b""))
        gps = ex.get("GPS", {})
        lat = _dms_to_dd(gps[piexif.GPSIFD.GPSLatitude], gps[piexif.GPSIFD.GPSLatitudeRef])
        lon = _dms_to_dd(gps[piexif.GPSIFD.GPSLongitude], gps[piexif.GPSIFD.GPSLongitudeRef])
        if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        return {"lat": lat, "lon": lon}
    except Exception:
        return None

@router.post("/api/batch/upload")
async def batch_upload(files: List[UploadFile] = File(...)):
    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(413, f"Максимум {MAX_BATCH_FILES} файлов за запрос")
    results = []
    total_bytes = 0
    for file in files:
        try:
            remaining = MAX_BATCH_BYTES - total_bytes
            if remaining <= 0:
                raise HTTPException(413, f"Пакет превышает лимит {MAX_BATCH_BYTES // (1024 * 1024)} МБ")
            data = await file.read(min(MAX_FILE_BYTES, remaining) + 1)
            total_bytes += len(data)
            if len(data) > min(MAX_FILE_BYTES, remaining):
                raise HTTPException(413, "Превышен лимит размера файла или пакета")
            if not data:
                results.append({"filename": file.filename, "lat": None, "lon": None, "error": "Пустой файл"})
                continue
            coords = extract_gps_safe(data)
            results.append({"filename": file.filename, **(coords or {"lat": None, "lon": None}),
                            "error": None if coords else "Нет GPS в EXIF"})
        except HTTPException:
            raise
        except Exception:
            results.append({"filename": file.filename, "lat": None, "lon": None, "error": "Ошибка обработки файла"})
        finally:
            await file.close()
    return results
