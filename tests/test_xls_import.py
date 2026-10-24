"""XLS/XLSX parsing and installed-points API tests."""

import io
import sys
import uuid
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main
import xls_import


def _xlsx(rows, header=("Город", "Адрес", "Сторона", "Координаты")):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Точки"
    sheet.append(["Служебный заголовок"])
    sheet.append([])
    sheet.append(list(header))
    for row in rows:
        sheet.append(list(row))
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _admin() -> TestClient:
    client = TestClient(main.app)
    response = client.post(
        "/api/auth/login", json={"login": "админ", "password": "альбион"}
    )
    assert response.status_code == 200, response.text
    return client


def test_parses_xlsx_header_below_title_and_normalizes_rows():
    data = _xlsx([
        ("Ростов-на-Дону", "ул. Ленина, 1", "А", "47.237201,39.816711"),
        ("Ростов-на-Дону", "ул. Ленина, 1", "B", "47.237201, 39.816711"),
        ("Азов", "Петровская, 2", "A", "47,100000 39,400000"),
        ("", "Без координат", "", ""),
        ("Азов", "Плохие координаты", "A", "999, 999"),
    ], header=("Город", "Адрес", "Сторона", "Координаты (WGS84)"))

    parsed = xls_import.parse_workbook(data, "points.xlsx")

    assert len(parsed.points) == 3
    assert parsed.points[0]["side"] == "A"
    assert parsed.points[2]["lat"] == pytest.approx(47.1)
    assert parsed.points[2]["lon"] == pytest.approx(39.4)
    assert parsed.skipped >= 2
    assert parsed.errors


def test_parses_legacy_xls_fixture():
    path = Path(__file__).parent / "data" / "installed_sample.xls"
    parsed = xls_import.parse_workbook(path.read_bytes(), path.name)
    assert len(parsed.points) == 3
    assert parsed.points[0]["city"] == "Ростов-на-Дону"
    assert parsed.points[0]["side"] == "A"
    assert parsed.points[2]["lat"] == pytest.approx(47.1)


def test_supports_separate_lat_lon_columns():
    data = _xlsx([
        ("Ростов-на-Дону", "ул. Мира, 3", "С", 47.25, 39.75),
    ], header=("Город", "Адрес", "Сторона", "Широта", "Долгота"))
    parsed = xls_import.parse_workbook(data, "points.xlsx")
    assert len(parsed.points) == 1
    assert parsed.points[0]["side"] == "C"
    assert parsed.points[0]["lon"] == pytest.approx(39.75)


def test_import_is_idempotent_and_clearable():
    client = _admin()
    client.delete("/api/installed")
    data = _xlsx([
        ("Ростов-на-Дону", "ул. Ленина, 1", "А", "47.237201,39.816711"),
        ("Азов", "Петровская, 2", "A", "47,100000 39,400000"),
    ])

    first = client.post(
        "/api/installed/import",
        files={"file": ("points.xlsx", data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert first.status_code == 200, first.text
    assert first.json()["inserted"] == 2
    assert first.json()["total"] == 2

    second = client.post(
        "/api/installed/import",
        files={"file": ("points.xlsx", data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert second.status_code == 200, second.text
    assert second.json()["inserted"] == 0
    assert second.json()["updated"] == 2
    assert second.json()["total"] == 2

    listed = client.get("/api/installed")
    assert listed.status_code == 200
    assert listed.json()["count"] == 2
    assert listed.json()["points"][0]["side"] == "A"

    cleared = client.delete("/api/installed")
    assert cleared.status_code == 200
    assert cleared.json()["deleted"] == 2
    assert client.get("/api/installed").json()["count"] == 0


def test_import_requires_admin_and_excel_extension():
    admin = _admin()
    login_name = f"installed_editor_{uuid.uuid4().hex[:8]}"
    created = admin.post(
        "/api/users",
        json={"login": login_name, "password": "editor-pass", "name": "Редактор", "role": "editor"},
    )
    assert created.status_code == 200, created.text
    editor = TestClient(main.app)
    assert editor.post(
        "/api/auth/login", json={"login": login_name, "password": "editor-pass"}
    ).status_code == 200
    denied = editor.post(
        "/api/installed/import",
        files={"file": ("points.xlsx", _xlsx([]), "application/octet-stream")},
    )
    assert denied.status_code == 403

    invalid = admin.post(
        "/api/installed/import",
        files={"file": ("points.csv", b"a,b,c", "text/csv")},
    )
    assert invalid.status_code == 422


def test_reads_xlsx_with_corrupt_stylesheet():
    """Site exports can have invalid styles.xml; cell data must still import."""
    original = _xlsx([
        ("Ростов-на-Дону", "ул. Ленина, 1", "А", "47.237201,39.816711"),
    ])
    source = zipfile.ZipFile(io.BytesIO(original))
    broken = io.BytesIO()
    with zipfile.ZipFile(broken, "w") as target:
        for item in source.infolist():
            payload = source.read(item.filename)
            if item.filename == "xl/styles.xml":
                payload = b"<broken"
            target.writestr(item, payload)
    source.close()

    parsed = xls_import.parse_workbook(broken.getvalue(), "broken.xlsx")

    assert len(parsed.points) == 1
    assert parsed.points[0]["lat"] == pytest.approx(47.237201)


def _ods_bytes() -> bytes:
    content = '''<?xml version="1.0" encoding="UTF-8"?>
<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:body><office:spreadsheet>
  <table:table table:name="Точки">
   <table:table-row>
    <table:table-cell office:value-type="string"><text:p>Город</text:p></table:table-cell>
    <table:table-cell office:value-type="string"><text:p>Адрес</text:p></table:table-cell>
    <table:table-cell office:value-type="string"><text:p>Сторона</text:p></table:table-cell>
    <table:table-cell office:value-type="string"><text:p>Координаты</text:p></table:table-cell>
   </table:table-row>
   <table:table-row>
    <table:table-cell office:value-type="string"><text:p>Ростов-на-Дону</text:p></table:table-cell>
    <table:table-cell office:value-type="string"><text:p>ул. Ленина, 1</text:p></table:table-cell>
    <table:table-cell office:value-type="string"><text:p>А</text:p></table:table-cell>
    <table:table-cell office:value-type="string"><text:p>47.237201,39.816711</text:p></table:table-cell>
   </table:table-row>
  </table:table>
 </office:spreadsheet></office:body>
</office:document-content>'''
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        archive.writestr("content.xml", content)
    return output.getvalue()


def test_parses_ods_directly():
    parsed = xls_import.parse_workbook(_ods_bytes(), "points.ods")
    assert len(parsed.points) == 1
    assert parsed.points[0]["city"] == "Ростов-на-Дону"
    assert parsed.points[0]["side"] == "A"
    assert parsed.points[0]["lat"] == pytest.approx(47.237201)


def test_imports_construction_format_for_shield_filter():
    data = _xlsx([
        ("Ростов-на-Дону", "ул. Ленина, 1", "А", "47.237201,39.816711", "Щит"),
    ], header=("Город", "Адрес", "Сторона", "Координаты", "Формат конструкции"))
    parsed = xls_import.parse_workbook(data, "points.xlsx")
    assert parsed.points[0]["format"] == "Щит"
