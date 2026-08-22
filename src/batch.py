"""
Пакетная обработка фото: извлечение GPS из множества файлов
"""
from fastapi import APIRouter, UploadFile, File
from typing import List
import io
from PIL import Image
import piexif
from fractions import Fraction

router = APIRouter()

def _dms_to_dd(dms: tuple, ref: bytes) -> float:
    d, m, s = (float(Fraction(x[0], x[1])) for x in dms)
    return -(d + m / 60 + s / 3600) if ref in (b"S", b"W") else (d + m / 60 + s / 3600)

def extract_gps_safe(data: bytes) -> dict | None:
    try:
        img = Image.open(io.BytesIO(data))
        ex = piexif.load(img.info.get("exif", b""))
        gps = ex.get("GPS", {})
        if piexif.GPSIFD.GPSLatitude not in gps:
            return None
        return {
            "lat": _dms_to_dd(gps[piexif.GPSIFD.GPSLatitude], gps[piexif.GPSIFD.GPSLatitudeRef]),
            "lon": _dms_to_dd(gps[piexif.GPSIFD.GPSLongitude], gps[piexif.GPSIFD.GPSLongitudeRef])
        }
    except Exception:
        return None

@router.post("/api/batch/upload")
async def batch_upload(files: List[UploadFile] = File(...)):
    results = []
    for file in files:
        try:
            data = await file.read()
            if not data:
                results.append({"filename": file.filename, "lat": None, "lon": None, "error": "Пустой файл"})
                continue
            
            coords = extract_gps_safe(data)
            if coords:
                results.append({
                    "filename": file.filename,
                    "lat": coords["lat"],
                    "lon": coords["lon"],
                    "error": None
                })
            else:
                results.append({"filename": file.filename, "lat": None, "lon": None, "error": "Нет GPS в EXIF"})
        except Exception as e:
            results.append({"filename": file.filename, "lat": None, "lon": None, "error": str(e)})
            
    return results
