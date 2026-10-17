import asyncio
import base64
import math
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main

client = TestClient(main.app)
_login = client.post("/api/auth/login", json={"login": "админ", "password": "альбион"})
assert _login.status_code == 200, _login.text


def test_project_linestring_projects_to_nearest_segment():
    line = [[39.70, 47.20], [39.72, 47.20], [39.74, 47.20]]
    projected = main._project_on_line(line, 39.72, 47.201)
    assert projected is not None
    assert projected["perpendicular_m"] == pytest.approx(111.2, abs=2)
    assert projected["t"] == pytest.approx(0.5, abs=0.01)


def test_road_side_info_accepts_linestring_and_multilinestring():
    feature = {"geometry": {"type": "LineString", "coordinates": [[39.70, 47.20], [39.74, 47.20]]}, "properties": {}}
    info = main._road_side_info(feature, 39.72, 47.201)
    assert "error" not in info
    multi = {"geometry": {"type": "MultiLineString", "coordinates": [[], [[39.70, 47.20], [39.74, 47.20]]]}, "properties": {}}
    assert "error" not in main._road_side_info(multi, 39.72, 47.201)


@pytest.mark.parametrize("coords", [[], [[39.7, 47.2]], [["bad", 47.2], [39.8, 47.2]]])
def test_invalid_lines_are_ignored(coords):
    feature = {"geometry": {"type": "LineString", "coordinates": coords}, "properties": {}}
    assert main._road_side_info(feature, 39.72, 47.201) == {"error": "no line"}


def test_federal_code_uses_token_and_transliteration():
    assert main.find_federal_road("трасса М-4 Дон")
    assert main.find_federal_road("trassa M-4")
    assert main.find_federal_road("М-40") is None


def test_fuzzy_empty_name_does_not_match_first_road():
    assert main.find_road_id_by_name("---") is None


def test_dms_rejects_invalid_minutes_and_ref():
    with pytest.raises(ValueError):
        main._dms_to_dd(((1, 1), (60, 1), (0, 1)), b"N")
    with pytest.raises(ValueError):
        main._dms_to_dd(((1, 1), (0, 1), (0, 1)), b"X")


def test_bbox_is_bounded_at_pole():
    box = main._bbox_from_radius(90, 179, 5000)
    assert box[0] >= -180 and box[2] <= 180
    assert box[1] >= -90 and box[3] <= 90


@pytest.mark.parametrize("body", [
    {"lat": float("nan"), "lon": 39.7},
    {"lat": 91, "lon": 39.7},
    {"lat": 47.2, "lon": 181},
    {"lat": 47.2, "lon": 39.7, "radius": 5001},
])
def test_locate_rejects_invalid_coordinates_and_radius(body):
    if isinstance(body.get("lat"), float) and math.isnan(body["lat"]):
        response = client.post("/api/locate", content='{"lat":NaN,"lon":39.7}', headers={"content-type": "application/json"})
    else:
        response = client.post("/api/locate", json=body)
    assert response.status_code == 422


def test_health_does_not_disclose_service_configuration():
    assert client.get("/api/health").json() == {"status": "ok"}


def test_mutating_api_rejects_cross_origin_requests():
    response = client.post("/api/bookmarks", json={"name": "test", "lat": 47.2, "lon": 39.7},
                           headers={"origin": "https://attacker.example"})
    assert response.status_code == 403


def test_mutating_api_allows_same_origin_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "BOOKMARKS_FILE", tmp_path / "bookmarks.json")
    response = client.post("/api/bookmarks", json={"name": "test", "lat": 47.2, "lon": 39.7},
                           headers={"origin": "http://127.0.0.1:8019"})
    assert response.status_code == 200


def test_static_assets_allowlist_only():
    assert client.get("/static/rostov_boundary.geojson").status_code == 200
    assert client.get("/static/main.py").status_code == 404
    assert client.get("/static/index.html.save").status_code == 404


def test_rejects_declared_oversized_request_body():
    response = client.post("/api/locate", content="{}", headers={"content-length": str(main.MAX_REQUEST_BODY_BYTES + 1)})
    assert response.status_code == 413


def test_screenshot_rejects_path_traversal_and_invalid_png(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "PROJECT_DIR", tmp_path)
    assert client.post("/api/save-screenshot", json={"image": "AAAA", "filename": "../owned.png"}).status_code == 422
    assert client.post("/api/save-screenshot", json={"image": "AAAA", "filename": "ok.png"}).status_code == 415
    assert not (tmp_path.parent / "owned.png").exists()


def test_screenshot_write_confined_to_screenshots(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "PROJECT_DIR", tmp_path)
    image = Image.new("RGB", (2, 2), "white")
    import io
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    data = base64.b64encode(buffer.getvalue()).decode("ascii")
    response = client.post("/api/save-screenshot", json={"image": data, "filename": "ok.png", "save_path": "screenshots"})
    assert response.status_code == 200
    assert (tmp_path / "screenshots" / "ok.png").is_file()


def test_exif_endpoint_rejects_oversized_file():
    response = client.post("/api/exif", files={"photo": ("big.jpg", b"x" * (12 * 1024 * 1024 + 1), "image/jpeg")})
    assert response.status_code == 413


def test_batch_rejects_too_many_files():
    files = [("files", (f"{i}.jpg", b"x", "image/jpeg")) for i in range(main.batch_router.routes[0].endpoint.__globals__["MAX_BATCH_FILES"] + 1)]
    response = client.post("/api/batch/upload", files=files)
    assert response.status_code == 413


def test_wfs_errors_are_gateway_errors():
    class FailedClient:
        async def get(self, *args, **kwargs):
            raise main.httpx.ConnectError("offline")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main._wfs_getfeature(FailedClient(), main.ROAD_LAYER))
    assert exc.value.status_code == 502


def test_location_returns_502_on_wfs_outage(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(main, "_wfs_getfeature", AsyncMock(side_effect=HTTPException(502, "offline")))
    response = client.post("/api/locate", json={"lat": 47.2, "lon": 39.7})
    assert response.status_code == 502


def test_location_picks_closest_line_geometry_and_returns_it(monkeypatch, tmp_path):
    feature_near = {"geometry": {"type": "LineString", "coordinates": [[39.70, 47.20], [39.74, 47.20]]},
                    "properties": {"road_name": "М-4 Дон", "category_gid": "1", "start_km": "1", "finish_km": "3"}}
    feature_far = {"geometry": {"type": "LineString", "coordinates": [[39.7, 47.25], [39.8, 47.25]]},
                   "properties": {"road_name": "Р-260", "category_gid": 1}}
    monkeypatch.setattr(main, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(main, "_wfs_getfeature", AsyncMock(return_value=[feature_far, feature_near]))
    result = client.post("/api/locate", json={"lat": 47.201, "lon": 39.72, "radius": 2000}).json()
    assert result["road"]["full_name"].startswith("М-4")
    assert result["road"]["geometry"]["type"] == "LineString"
    assert result["road"]["category"] == "Автомагистраль"
    assert result["side"]["km_estimate"] == pytest.approx(2.0, abs=0.1)
