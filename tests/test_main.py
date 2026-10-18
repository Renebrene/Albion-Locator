import asyncio
import base64
import math
import sqlite3
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


def test_road_name_match_keeps_exact_code_and_rejects_unknown_code(monkeypatch):
    monkeypatch.setattr(main, "ROADS", {
        "М-4": "М-4 Дон",
        "Р-260": "Волгоград Каменск Шахтинский Луганск",
    })
    assert main.find_road_id_by_name("M-4") == "М-4"
    assert main.find_road_id_by_name("Х-999") is None


def test_road_name_match_uses_exact_name_and_rejects_short_false_positive(monkeypatch):
    monkeypatch.setattr(main, "ROADS", {
        "60К-1": "автомобильная дорога поселок Зеленый",
        "60К-2": "автомобильная дорога поселок Зеленый хутор Красный",
    })
    assert main.find_road_id_by_name("автомобильная дорога поселок Зеленый") == "60К-1"
    assert main.find_road_id_by_name("Зеленый") is None
    assert main.find_road_id_by_name("дорога") is None


def test_road_name_match_returns_none_for_ambiguous_candidates(monkeypatch):
    monkeypatch.setattr(main, "ROADS", {
        "60К-1": "автомобильная дорога поселок Зеленый хутор Красный",
        "60К-2": "автомобильная дорога поселок Зеленый хутор Синий",
    })
    assert main.find_road_id_by_name("дорога поселок Зеленый хутор") is None


def test_format_road_names_preserves_unmatched_skdf_name_without_llm(monkeypatch):
    monkeypatch.setattr(main, "ROADS", {"60К-1": "автомобильная дорога поселок Зеленый"})

    async def unexpected_llm(_):
        raise AssertionError("LLM must not guess a public road name")

    monkeypatch.setattr(main, "find_road_id_with_llm", unexpected_llm)
    result = asyncio.run(main.format_road_names({
        "road_name": "улица Незнакомая",
        "value_of_the_road": None,
    }))
    assert result == {"skdf_name": "улица Незнакомая", "full_name": "улица Незнакомая"}


def test_format_road_names_does_not_fuzzy_match_over_unknown_explicit_code(monkeypatch):
    monkeypatch.setattr(main, "ROADS", {"60К-1": "автомобильная дорога поселок Зеленый"})
    result = asyncio.run(main.format_road_names({
        "road_name": "Х-999 поселок Зеленый",
        "value_of_the_road": "Х-999",
    }))
    assert result["full_name"] == "Х-999 поселок Зеленый"


def test_format_road_names_prefers_explicit_road_code(monkeypatch):
    monkeypatch.setattr(main, "ROADS", {
        "М-4": "М-4 Дон",
        "Р-260": "Волгоград Каменск Шахтинский Луганск",
    })
    result = asyncio.run(main.format_road_names({
        "road_name": "улица Мира",
        "value_of_the_road": "Автомобильная дорога M-4 Дон",
    }))
    assert result["full_name"] == "М-4, М-4 Дон"


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


def test_wfs_paginates_bbox_and_collects_all_features(monkeypatch):
    monkeypatch.setattr(main, "WFS_PAGE_SIZE", 2)
    pages = [
        {"features": [{"id": 1}, {"id": 2}], "numberReturned": 2, "numberMatched": 3},
        {"features": [{"id": 3}], "numberReturned": 1, "numberMatched": 3},
    ]
    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    class PagedClient:
        async def get(self, url, params, timeout):
            calls.append(params)
            return Response(pages.pop(0))

    result = asyncio.run(main._wfs_getfeature(
        PagedClient(), main.ROAD_LAYER, bbox_deg=(39.6, 47.1, 39.8, 47.3)
    ))
    assert result == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert [call["startIndex"] for call in calls] == ["0", "2"]
    assert all(call["count"] == "2" for call in calls)
    assert all(call["version"] == "2.0.0" for call in calls)
    assert all(call["sortBy"] == "id A" for call in calls)
    assert all(call["bbox"].startswith("39.6,47.1,39.8,47.3,") for call in calls)


def test_wfs_stops_after_short_page():
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"features": [{"id": 1}], "numberReturned": 1, "numberMatched": "unknown"}

    class OnePageClient:
        async def get(self, *args, **kwargs):
            calls.append(kwargs)
            return Response()

    result = asyncio.run(main._wfs_getfeature(OnePageClient(), main.ROAD_LAYER))
    assert result == [{"id": 1}]
    assert len(calls) == 1


def test_wfs_rejects_repeated_page():
    page = {"features": [{"id": 1}, {"id": 2}], "numberReturned": 2, "numberMatched": 4}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return page

    class RepeatingClient:
        async def get(self, *args, **kwargs):
            return Response()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(main._wfs_getfeature(RepeatingClient(), main.ROAD_LAYER, max_features=2))
    assert exc.value.status_code == 502


def test_wfs_fails_instead_of_returning_truncated_results(monkeypatch):
    monkeypatch.setattr(main, "WFS_PAGE_SIZE", 2)
    monkeypatch.setattr(main, "WFS_MAX_FEATURES", 2)

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"features": [{"id": 1}, {"id": 2}], "numberReturned": 2, "numberMatched": 3}

    class TooManyClient:
        async def get(self, *args, **kwargs):
            return Response()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(main._wfs_getfeature(TooManyClient(), main.ROAD_LAYER))
    assert exc.value.status_code == 502


def _build_local_geo_index(path: Path) -> None:
    package_src = Path(main.GEO_PACKAGE_SRC)
    sys.path.insert(0, str(package_src)) if str(package_src) not in sys.path else None
    from offline_geo import build_index

    build_index(
        path,
        settlements=[{"id": "s1", "name": "Ростов-на-Дону"}],
        streets=[{"id": "st1", "settlement_id": "s1", "name": "Большая Садовая"}],
        houses=[{"id": "h1", "street_id": "st1", "house_number": "12", "lon": 39.71, "lat": 47.22}],
        roads=[{
            "id": "r1", "name": "М-4 Дон", "ref": "М-4", "km_start": 0, "km_end": 10,
            "geometry": [[39.70, 47.20], [39.74, 47.20]],
            "source": "test-fixture", "km_provenance": "estimated from geometry",
        }],
        manifest={"source": "synthetic test data"},
    )


def test_data_sources_reports_local_assets(tmp_path, monkeypatch):
    monkeypatch.setenv("ALBION_GEO_DATA_DIR", str(tmp_path))
    response = client.get("/api/data-sources")
    assert response.status_code == 200
    payload = response.json()
    assert payload["local_available"] is False
    assert payload["local_tiles_available"] is False
    assert payload["address_suggest_available"] is bool(main.YANDEX_SUGGEST_API_KEY)
    assert payload["suggest_view_available"] is bool(main.YANDEX_JS_API_KEY and main.YANDEX_SUGGEST_API_KEY)
    assert payload["address_geocoder_available"] is bool(main.YANDEX_API_KEY)
    assert payload["fully_offline_ready"] is False


def test_maps_config_is_authenticated_no_store_and_never_exposes_geocoder_key(monkeypatch):
    monkeypatch.setattr(main, "YANDEX_JS_API_KEY", "test-js-client-key")
    monkeypatch.setattr(main, "YANDEX_SUGGEST_API_KEY", "test-suggest-client-key")
    monkeypatch.setattr(main, "YANDEX_API_KEY", "server-only-geocoder-secret")

    response = client.get("/api/maps-config")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "suggest_view_available": True,
        "geocoder_available": True,
        "javascript_api_key": "test-js-client-key",
        "suggest_api_key": "test-suggest-client-key",
    }
    assert "server-only-geocoder-secret" not in response.text

    unauthenticated = TestClient(main.app).get("/api/maps-config")
    assert unauthenticated.status_code == 401


def test_offline_address_and_road_search_use_local_index(tmp_path, monkeypatch):
    database = tmp_path / "offline_geo.sqlite3"
    _build_local_geo_index(database)
    monkeypatch.setenv("ALBION_GEO_DATA_DIR", str(tmp_path))

    settlements = client.get("/api/address/suggest", params={"q": "Рост"})
    streets = client.get("/api/address/suggest", params={"q": "Сад", "settlement": "s1"})
    houses = client.get("/api/address/suggest", params={"q": "12", "settlement": "s1", "street": "st1"})
    roads = client.get("/api/roads/search", params={"q": "М-4", "km": "5+000"})

    assert settlements.status_code == 200 and settlements.json()["suggestions"][0]["id"] == "s1"
    assert streets.status_code == 200 and streets.json()["suggestions"][0]["id"] == "st1"
    assert houses.status_code == 200 and houses.json()["suggestions"][0]["lon"] == pytest.approx(39.71)
    assert roads.status_code == 200 and roads.json()["roads"][0]["km_provenance"] == "estimated from geometry"
    assert roads.json()["roads"][0]["selected_km"] == pytest.approx(5.0)


def _stub_http_response(payload, status_code=200):
    class Response:
        def __init__(self):
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                request = main.httpx.Request("GET", "https://test.invalid")
                response = main.httpx.Response(self.status_code, request=request)
                raise main.httpx.HTTPStatusError("upstream error", request=request, response=response)

        def json(self):
            return payload

    return Response()


def _stub_http_client(monkeypatch, response=None, error=None, requests=None):
    class HTTPClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, *, params=None, **kwargs):
            if requests is not None:
                requests.append((url, params))
            if error:
                raise error
            return response

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: HTTPClient())


def test_online_address_suggest_normalizes_and_bounds_yandex_results(monkeypatch):
    monkeypatch.setattr(main, "YANDEX_SUGGEST_API_KEY", "test-suggest-key")
    monkeypatch.setattr(main, "ADDRESS_SUGGEST_MIN_INTERVAL", 0)
    monkeypatch.setattr(main, "_ADDRESS_SUGGEST_LAST_REQUEST", {})
    requests = []
    _stub_http_client(monkeypatch, _stub_http_response({"results": [
        {"title": {"text": "Ростов-на-Дону"}, "subtitle": {"text": "Ростовская область, Россия"}, "uri": "ymapsbm1://town"},
        {"title": {"text": "Батайск"}, "subtitle": {"text": "Ростовская область, Россия"}, "uri": "ymapsbm1://city"},
        {"title": {"text": "Таганрог"}, "subtitle": {"text": "Ростовская область, Россия"}},
    ]}), requests=requests)

    response = client.post("/api/address/online/suggest", json={"query": "Ростов", "limit": 2})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source"] == "Yandex Geosuggest"
    assert [item["title"] for item in payload["suggestions"]] == ["Ростов-на-Дону", "Батайск"]
    assert payload["suggestions"][0]["query"] == "Ростов-на-Дону, Ростовская область, Россия"
    assert "test-suggest-key" not in response.text
    assert requests[0][1]["bbox"] == main.ROSTOV_SEARCH_BBOX
    assert requests[0][1]["results"] == 2


def test_online_address_suggest_requires_key_and_valid_query(monkeypatch):
    monkeypatch.setattr(main, "YANDEX_SUGGEST_API_KEY", "")
    monkeypatch.setattr(main, "ADDRESS_SUGGEST_MIN_INTERVAL", 0)
    monkeypatch.setattr(main, "_ADDRESS_SUGGEST_LAST_REQUEST", {})
    missing_key = client.post("/api/address/online/suggest", json={"query": "Ростов"})
    short_query = client.post("/api/address/online/suggest", json={"query": "Ро"})
    assert missing_key.status_code == 503
    assert "YANDEX_SUGGEST_API_KEY" in missing_key.json()["detail"]
    assert short_query.status_code == 422


def test_online_address_suggest_handles_yandex_timeout_and_rate_limit(monkeypatch):
    monkeypatch.setattr(main, "YANDEX_SUGGEST_API_KEY", "test-suggest-key")
    monkeypatch.setattr(main, "ADDRESS_SUGGEST_MIN_INTERVAL", 0)
    monkeypatch.setattr(main, "_ADDRESS_SUGGEST_LAST_REQUEST", {})
    _stub_http_client(monkeypatch, error=main.httpx.TimeoutException("timeout"))
    timeout = client.post("/api/address/online/suggest", json={"query": "Ростов"})
    assert timeout.status_code == 504

    monkeypatch.setattr(main, "_ADDRESS_SUGGEST_LAST_REQUEST", {})
    _stub_http_client(monkeypatch, _stub_http_response({}, status_code=429))
    limited = client.post("/api/address/online/suggest", json={"query": "Ростов"})
    assert limited.status_code == 429

    monkeypatch.setattr(main, "_ADDRESS_SUGGEST_LAST_REQUEST", {})
    _stub_http_client(monkeypatch, _stub_http_response({"unexpected": []}))
    invalid = client.post("/api/address/online/suggest", json={"query": "Ростов"})
    assert invalid.status_code == 502


def test_online_address_suggest_is_throttled_per_user(monkeypatch):
    monkeypatch.setattr(main, "YANDEX_SUGGEST_API_KEY", "test-suggest-key")
    monkeypatch.setattr(main, "ADDRESS_SUGGEST_MIN_INTERVAL", 10)
    monkeypatch.setattr(main, "_ADDRESS_SUGGEST_LAST_REQUEST", {})
    _stub_http_client(monkeypatch, _stub_http_response({"results": []}))

    first = client.post("/api/address/online/suggest", json={"query": "Ростов"})
    second = client.post("/api/address/online/suggest", json={"query": "Ростов-на-Дону"})
    assert first.status_code == 200
    assert second.status_code == 429


def test_online_address_geocode_returns_only_rostov_region_points(monkeypatch):
    monkeypatch.setattr(main, "YANDEX_API_KEY", "test-geocoder-key")
    monkeypatch.setattr(main, "ADDRESS_SUGGEST_MIN_INTERVAL", 0)
    requests = []
    _stub_http_client(monkeypatch, _stub_http_response({"response": {"GeoObjectCollection": {"featureMember": [{
        "GeoObject": {
            "Point": {"pos": "39.701 47.218"},
            "metaDataProperty": {"GeocoderMetaData": {
                "text": "Ростов-на-Дону, улица, 1", "precision": "exact",
                "Address": {"Components": [{"kind": "province", "name": "Ростовская область"}]},
            }},
        }
    }]}}}), requests=requests)
    response = client.post("/api/address/online/geocode", json={"query": "Ростов-на-Дону, улица, 1"})
    assert response.status_code == 200, response.text
    assert response.json()["lat"] == pytest.approx(47.218)
    assert response.json()["lon"] == pytest.approx(39.701)
    assert response.json()["precision"] == "exact"
    assert requests[0][1]["bbox"] == main.ROSTOV_SEARCH_BBOX
    assert requests[0][1]["rspn"] == 1
    assert "Ростовская область" in requests[0][1]["geocode"]

    monkeypatch.setattr(main, "_ADDRESS_SUGGEST_LAST_REQUEST", {})
    _stub_http_client(monkeypatch, _stub_http_response({"response": {"GeoObjectCollection": {"featureMember": [{
        "GeoObject": {"Point": {"pos": "30 55"}, "metaDataProperty": {"GeocoderMetaData": {"text": "Москва"}}}
    }]}}}))
    outside = client.post("/api/address/online/geocode", json={"query": "Москва"})
    assert outside.status_code == 404

    monkeypatch.setattr(main, "_ADDRESS_SUGGEST_LAST_REQUEST", {})
    _stub_http_client(monkeypatch, _stub_http_response({"response": {"GeoObjectCollection": {"featureMember": [{
        "GeoObject": {
            "Point": {"pos": "39.1 47.2"},
            "metaDataProperty": {"GeocoderMetaData": {
                "text": "Соседний регион", "precision": "exact",
                "Address": {"Components": [{"kind": "province", "name": "Краснодарский край"}]},
            }},
        }
    }]}}}))
    wrong_region = client.post("/api/address/online/geocode", json={"query": "Соседний регион"})
    assert wrong_region.status_code == 404


def test_offline_locate_does_not_fall_back_to_wfs(tmp_path, monkeypatch):
    _build_local_geo_index(tmp_path / "offline_geo.sqlite3")
    monkeypatch.setenv("ALBION_GEO_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(main, "_wfs_getfeature", AsyncMock(side_effect=AssertionError("offline mode must not call WFS")))
    response = client.post("/api/locate", json={
        "lat": 47.201, "lon": 39.72, "radius": 2000, "use_local": True,
    })
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["road"]["source"] == "test-fixture"
    assert payload["road"]["category_filter_applied"] is False
    assert payload["side"]["km_provenance"] == "estimated from geometry"
    assert payload["side"]["side"] is None
    assert payload["side"]["side_unknown"] is True


def test_offline_locate_fails_closed_without_local_database(tmp_path, monkeypatch):
    monkeypatch.setenv("ALBION_GEO_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(main, "_wfs_getfeature", AsyncMock(side_effect=AssertionError("must not fall back to WFS")))
    response = client.post("/api/locate", json={
        "lat": 47.201, "lon": 39.72, "radius": 2000, "use_local": True,
    })
    assert response.status_code == 503
    assert "не установлена" in response.json()["detail"]


def test_offline_tile_route_reads_mbtiles_with_xyz_to_tms_conversion(tmp_path, monkeypatch):
    mbtiles = tmp_path / "tiles.mbtiles"
    with sqlite3.connect(mbtiles) as database:
        database.execute("CREATE TABLE tiles(zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
        # z=2, x=1, XYZ y=1 maps to TMS row 2.
        database.execute("INSERT INTO tiles VALUES(2,1,2,?)", (b"fake-png",))
    monkeypatch.setattr(main, "_mbtiles_path", lambda: mbtiles)
    tile = client.get("/tiles/2/1/1.png")
    absent = client.get("/tiles/2/1/2.png")
    invalid = client.get("/tiles/2/5/1.png")
    assert tile.status_code == 200
    assert tile.headers["content-type"] == "image/png"
    assert tile.content == b"fake-png"
    assert absent.status_code == 404
    assert invalid.status_code == 404


def test_offline_tile_route_returns_clear_unavailable_when_no_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_mbtiles_path", lambda: tmp_path / "missing.mbtiles")
    response = client.get("/tiles/1/0/0.png")
    assert response.status_code == 503
    assert "not installed" in response.json()["detail"]


def test_msk61_api_returns_preliminary_transform_with_confirm_gate():
    response = client.post("/api/msk61/from-geographic", json={
        "lat": 47.236947, "lon": 39.673323, "zone": 2, "profile": "gost_51794_2008",
    })
    assert response.status_code == 200, response.text
    projected = response.json()
    assert projected["status"] == "preliminary_unverified"
    assert "verified" in projected["warning"].lower()
    assert projected["x"] == pytest.approx(423461.477, abs=0.02)
    assert projected["y"] == pytest.approx(2200918.281, abs=0.02)

    back = client.post("/api/msk61/to-geographic", json={
        "x": projected["x"], "y": projected["y"], "zone": 2, "profile": "gost_51794_2008",
    })
    assert back.status_code == 200, back.text
    assert back.json()["can_place_on_map"] is True
    assert back.json()["lat"] == pytest.approx(47.236947, abs=1e-6)
    assert back.json()["lon"] == pytest.approx(39.673323, abs=1e-6)

    calculated = client.post("/api/msk61/to-geographic", json={
        "x": projected["x"], "y": projected["y"], "zone": 2, "profile": "calculated",
    })
    assert calculated.status_code == 200
    assert calculated.json()["can_place_on_map"] is False


def test_protected_offline_data_endpoints_require_auth():
    unauthenticated = TestClient(main.app)
    assert unauthenticated.get("/api/data-sources").status_code == 401
    assert unauthenticated.get("/api/address/suggest", params={"q": "Рост"}).status_code == 401
    assert unauthenticated.post("/api/address/online/suggest", json={"query": "Ростов"}).status_code == 401
    assert unauthenticated.post("/api/address/online/geocode", json={"query": "Ростов-на-Дону"}).status_code == 401
    assert unauthenticated.get("/api/roads/search", params={"q": "М-4"}).status_code == 401


def test_location_returns_502_on_wfs_outage(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(main, "_wfs_getfeature", AsyncMock(side_effect=HTTPException(502, "offline")))
    response = client.post("/api/locate", json={"lat": 47.2, "lon": 39.7})
    assert response.status_code == 502


def test_location_picks_closest_feature_from_later_wfs_page(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(main, "WFS_PAGE_SIZE", 1)
    far = {"geometry": {"type": "LineString", "coordinates": [[39.70, 47.22], [39.74, 47.22]]},
           "properties": {"road_name": "Дальняя дорога", "category_gid": 1}}
    near = {"geometry": {"type": "LineString", "coordinates": [[39.70, 47.20], [39.74, 47.20]]},
            "properties": {"road_name": "Ближайшая дорога", "category_gid": 1}}
    pages = [
        {"features": [far], "numberReturned": 1, "numberMatched": 2},
        {"features": [near], "numberReturned": 1, "numberMatched": 2},
    ]

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    class PagedClient:
        async def get(self, *args, **kwargs):
            return Response(pages.pop(0))

    class ClientContext:
        async def __aenter__(self):
            return PagedClient()

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: ClientContext())
    result = client.post("/api/locate", json={"lat": 47.201, "lon": 39.72, "radius": 2000}).json()
    assert result["road"]["skdf_name"] == "Ближайшая дорога"
    assert not pages


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
