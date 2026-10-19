"""Browser E2E smoke: search dock, positions, chainage gaps, accessibility."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")


def login(page, base_url):
    page.goto(base_url)
    page.fill("#authLogin", "админ")
    page.fill("#authPassword", "альбион")
    page.click("#authSubmit")
    page.wait_for_selector("body.authenticated")


def _search_geometry(page):
    return page.evaluate("""() => {
        const input = document.getElementById('modeInput1').getBoundingClientRect();
        const button = document.getElementById('placePointBtn').getBoundingClientRect();
        return {inputLeft: input.left, inputRight: input.right, inputWidth: input.width,
                buttonLeft: button.left, buttonWidth: button.width};
    }""")


def test_login_default_road_mode_and_statusbar_layout(page, app_server):
    login(page, app_server["base_url"])
    assert page.locator("#searchDock").get_attribute("data-position") == "bottom"
    assert page.locator("#modeRoadBtn").get_attribute("aria-pressed") == "true"
    assert page.locator("#searchDockBottomHost").evaluate("el => getComputedStyle(el).display") == "contents"
    assert page.locator("#statusPointText").is_hidden()
    geometry = _search_geometry(page)
    assert geometry["inputWidth"] > 500
    assert geometry["buttonLeft"] >= geometry["inputRight"] - 1
    assert not page._e2e_errors, page._e2e_errors


def test_top_position_restores_status_text_and_moves_zoom(page, app_server):
    login(page, app_server["base_url"])
    page.evaluate("""() => {
        const select = document.getElementById('searchPositionSelect');
        select.value = 'top';
        select.dispatchEvent(new Event('change'));
    }""")
    assert page.locator("#searchDock").evaluate("el => el.parentElement.id") == "searchDockTopHost"
    assert page.locator("#statusPointText").is_visible()
    geometry = _search_geometry(page)
    assert geometry["inputWidth"] > 400
    assert geometry["buttonLeft"] >= geometry["inputRight"] - 1
    zoom_parent = page.locator(".leaflet-control-zoom").evaluate("el => el.parentElement.className")
    assert "leaflet-bottom" in zoom_parent and "leaflet-left" in zoom_parent
    assert not page._e2e_errors, page._e2e_errors


def test_suggestions_open_upward_in_statusbar(page, app_server):
    login(page, app_server["base_url"])
    position = page.evaluate("""() => {
        const select = document.getElementById('searchPositionSelect');
        select.value = 'bottom';
        select.dispatchEvent(new Event('change'));
        const suggestions = document.getElementById('modeSuggestions');
        suggestions.classList.add('open');
        const fields = document.getElementById('modeFields').getBoundingClientRect();
        const rect = suggestions.getBoundingClientRect();
        const style = getComputedStyle(suggestions);
        return {fieldsTop: fields.top, suggestionTop: rect.top,
                top: style.top, bottom: style.bottom};
    }""")
    assert position["suggestionTop"] <= position["fieldsTop"]
    assert position["bottom"] != "auto"
    assert not page._e2e_errors, page._e2e_errors


def test_chainage_gap_and_overlap_state(page, app_server):
    login(page, app_server["base_url"])
    result = page.evaluate("""() => {
        const sections = [
            {start_km: 0, finish_km: 5, coordinates: [[39.70, 47.20], [39.74, 47.20]]},
            {start_km: 15, finish_km: 20, coordinates: [[39.80, 47.20], [39.84, 47.20]]}
        ];
        const gapState = buildRoadKmState({first_km: 0, last_km: 20, sections});
        const overlaps = buildRoadKmState({first_km: 0, last_km: 15, sections: [
            {start_km: 0, finish_km: 10, coordinates: [[39.70, 47.20], [39.74, 47.20]]},
            {start_km: 5, finish_km: 15, coordinates: [[39.72, 47.20], [39.76, 47.20]]}
        ]});
        // Проверяем, что попадание в разрыв очищает черновик километра.
        roadKmTitle.textContent = 'М-4';
        roadKmState = gapState;
        searchDraft.mode = 'road';
        modeInput2.value = '10';
        const preview = previewRoadKm(10);
        return {
            gaps: gapState.gaps,
            hasGaps: gapState.hasGaps,
            intervals: gapState.intervals,
            hasOverlaps: overlaps.hasOverlaps,
            overlapIntervals: overlaps.intervals,
            preview,
            draftLat: searchDraft.roadLat,
            modeInput: modeInput2.value,
            status: roadKmStatus.textContent
        };
    }""")
    assert result["hasGaps"] is True
    assert result["gaps"][0]["from"] == pytest.approx(5)
    assert result["gaps"][0]["to"] == pytest.approx(15)
    assert result["hasOverlaps"] is True
    assert result["overlapIntervals"] == [[0, 15]]
    assert result["preview"] is None
    assert result["draftLat"] is None
    assert result["modeInput"] == ""
    assert "разрыв" in result["status"]


def test_committed_map_point_syncs_official_km_slider(page, app_server):
    login(page, app_server["base_url"])
    result = page.evaluate("""async () => {
        apiFetch = async () => new Response(
            JSON.stringify({point: {lat: 47.2, lon: 39.8, bearing: null}, road: null, side: null}),
            {status: 200, headers: {'Content-Type': 'application/json'}}
        );
        roadKmTitle.textContent = 'М-4';
        roadKmState = buildRoadKmState({
            first_km: 0, last_km: 20,
            sections: [{
                start_km: 0, finish_km: 20,
                coordinates: [[39.70, 47.20], [39.90, 47.20]]
            }]
        });
        roadKmSlider.max = String(roadKmState.totalLengthKm);
        commitPoint(47.2, 39.8, 'click');
        await new Promise(resolve => setTimeout(resolve, 50));
        return {
            source: activePoint ? activePoint.source : null,
            slider: Number(roadKmSlider.value),
            kmField: roadKmNumber.value,
            status: roadKmStatus.textContent
        };
    }""")
    assert result["source"] == "click"
    assert result["slider"] == pytest.approx(10, abs=0.3)
    assert result["kmField"].startswith("10+")
    assert "синхронизирован" in result["status"]


def test_checkbox_starts_unchecked_and_first_activation_is_road(page, app_server):
    login(page, app_server["base_url"])
    page.evaluate("""async () => {
        const current = await (await fetch('/api/user/workspace')).json();
        await fetch('/api/user/workspace', {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({payload: {searchMode: 'coords', searchArmed: true}, version: current.version || 0})
        });
    }""")
    page.reload()
    page.wait_for_selector("body.authenticated")
    assert page.locator("#searchArmCheckbox").is_checked() is False
    assert page.locator("#modeInput1").is_disabled()
    page.check("#searchArmCheckbox")
    assert page.locator("#modeRoadBtn").get_attribute("aria-pressed") == "true"
    assert page.locator("#modeInput1").is_enabled()


def test_official_km_slider_syncs_to_point_from_skdf_geometry(page, app_server):
    login(page, app_server["base_url"])
    result = page.evaluate("""() => {
        roadKmTitle.textContent = 'М-4';
        roadKmState = buildRoadKmState({
            first_km: 0, last_km: 20,
            sections: [{
                start_km: 0, finish_km: 20,
                coordinates: [[39.70, 47.20], [39.90, 47.20]]
            }]
        });
        roadKmState.anchor = {lon: 39.80, lat: 47.20};
        roadKmSlider.max = String(roadKmState.totalLengthKm);
        const point = syncRoadKmSliderToPoint(39.80, 47.1999);
        return {
            point: point ? {lat: point.lat, lon: point.lon} : null,
            slider: Number(roadKmSlider.value),
            kmField: roadKmNumber.value,
            draftKm: searchDraft.roadKm,
            status: roadKmStatus.textContent
        };
    }""")
    assert result["point"] is not None
    assert result["slider"] == pytest.approx(10, abs=0.3)
    assert result["kmField"].startswith("10+")
    assert result["draftKm"] == pytest.approx(10, abs=0.3)
    assert "синхронизирован" in result["status"]


def test_visible_form_controls_have_labels(page, app_server):
    login(page, app_server["base_url"])
    unnamed = page.evaluate("""() => {
        return Array.from(document.querySelectorAll('input, select, textarea')).filter(el => {
            if (el.offsetParent === null || el.type === 'hidden') return false;
            if (el.getAttribute('aria-label') || el.getAttribute('aria-labelledby')) return false;
            if (el.id && document.querySelector(`label[for="${el.id}"]`)) return false;
            if (el.closest('label')) return false;
            if ((el.getAttribute('placeholder') || '').trim()) return false;
            return true;
        }).map(el => el.id || el.name || el.className || el.tagName);
    }""")
    assert unnamed == []


def test_visible_buttons_have_accessible_names(page, app_server):
    login(page, app_server["base_url"])
    unnamed = page.evaluate("""() => {
        const elements = Array.from(document.querySelectorAll('button, [role="button"], [role="switch"]'));
        return elements.filter(el => {
            if (el.offsetParent === null || el.disabled) return false;
            const name = el.getAttribute('aria-label') || el.getAttribute('title')
                || (el.textContent || '').trim();
            return !name;
        }).map(el => el.id || el.className || el.tagName);
    }""")
    assert unnamed == []
    assert not page._e2e_errors, page._e2e_errors
