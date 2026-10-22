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
    # Дать приложению применить preferences/workspace, иначе тест может
    # изменить состояние, которое затем перезапишет loadUserState().
    page.wait_for_function("uiState.ready", timeout=15000)


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
    assert page.locator("#roadKmEnableCheckbox").is_checked() is False
    assert page.locator("#roadKmSlider").is_disabled()
    assert page.locator("#roadKmNumber").input_value() == ""
    assert page.locator("#roadKmStatus").inner_text() == ""
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


def test_center_button_uses_point_or_all_batch_points(page, app_server):
    login(page, app_server["base_url"])
    page.wait_for_timeout(200)
    point_center = page.evaluate("""() => {
        activateFrameTab('point');
        activePoint = {lat: 47.5, lon: 40.0, source: 'test'};
        centerMapOnData();
        const center = map.getCenter();
        return {center: [center.lat, center.lng], zoom: map.getZoom()};
    }""")
    assert point_center["center"] == pytest.approx([47.5, 40.0], abs=0.01)
    assert point_center["zoom"] >= 16
    batch = page.evaluate("""() => {
        activateFrameTab('batch');
        batchPoints = [
            {id: 'a', lat: 46.8, lon: 39.0},
            {id: 'b', lat: 48.2, lon: 41.0}
        ];
        centerMapOnData();
        const bounds = map.getBounds();
        return {
            containsA: bounds.contains([46.8, 39.0]),
            containsB: bounds.contains([48.2, 41.0])
        };
    }""")
    assert batch == {"containsA": True, "containsB": True}


def test_ghost_point_moves_only_clone_and_apply_promotes_it(page, app_server):
    login(page, app_server["base_url"])
    result = page.evaluate("""async () => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        apiFetch = async () => ok({point: {lat: 47.2, lon: 39.8}, road: null, side: null});
        roadKmTitle.textContent = 'М-4';
        roadKmState = buildRoadKmState({
            first_km: 0, last_km: 20,
            sections: [{start_km: 0, finish_km: 20,
                        coordinates: [[39.70, 47.20], [39.90, 47.20]]}]
        });
        roadKmSlider.max = String(roadKmState.totalLengthKm);
        roadKmEnableCheckbox.checked = true;
        applyRoadKmActivation();
        commitPoint(47.2, 39.8, 'click');
        await new Promise(resolve => setTimeout(resolve, 80));
        const original = [activePoint.lat, activePoint.lon];
        ghostPointToggle.click();
        const slider = document.getElementById('roadKmSlider');
        slider.value = String(Number(slider.max) * 0.25);
        slider.dispatchEvent(new Event('input', {bubbles: true}));
        const during = {
            original: [activePoint.lat, activePoint.lon],
            ghost: ghostMarker ? [ghostLat, ghostLon] : null,
            marker: ghostMarker ? ghostMarker.getLatLng() : null,
            km: roadKmNumber.value
        };
        roadKmApplyButton.click();
        await new Promise(resolve => setTimeout(resolve, 80));
        return {
            original,
            during,
            after: {
                point: [activePoint.lat, activePoint.lon],
                ghost: ghostMarker,
                km: roadKmNumber.value,
                source: activePoint.source
            }
        };
    }""")
    assert result["during"]["original"] == pytest.approx(result["original"])
    assert result["during"]["ghost"] is not None
    assert result["during"]["marker"] is not None
    assert result["during"]["km"].startswith("5+")
    assert result["after"]["ghost"] is None
    assert result["after"]["point"] != pytest.approx(result["original"])
    assert result["after"]["source"] == "road-km"


def test_coords_field_wgs_available_msk_and_ghost_parentheses(page, app_server):
    login(page, app_server["base_url"])
    result = page.evaluate("""async () => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        const originalFetch = apiFetch;
        apiFetch = async (url, options) => {
            if (url === '/api/locate') return ok({point: {lat: 47.2, lon: 39.8}, road: null, side: null});
            return originalFetch(url, options);
        };
        crsState = 'gps';
        updateCrsLabel();
        commitPoint(47.2, 39.8, 'click');
        await new Promise(resolve => setTimeout(resolve, 150));
        searchArmCheckbox.checked = true;
        applySearchArmState();
        setSearchMode('coords');
        for (let i = 0; i < 40 && !activeMsk; i += 1) {
            await new Promise(resolve => setTimeout(resolve, 50));
        }
        const wgs = modeInput1.value;
        modeActionBtn.click();
        await new Promise(resolve => setTimeout(resolve, 80));
        const msk = modeInput1.value;
        const zone = Number(crsState.slice(3));
        const select = document.getElementById('mskZone');
        const wrongZone = zone === 1 ? 2 : 1;
        select.value = String(wrongZone);
        select.dispatchEvent(new Event('change', {bubbles: true}));
        const wrong = modeInput1.value;
        select.value = String(zone);
        select.dispatchEvent(new Event('change', {bubbles: true}));
        const correct = modeInput1.value;
        crsState = 'gps';
        updateCrsLabel();
        renderCoordsInput();
        roadKmState = buildRoadKmState({
            first_km: 0, last_km: 20,
            sections: [{start_km: 0, finish_km: 20,
                        coordinates: [[39.70, 47.20], [39.90, 47.20]]}]
        });
        roadKmSlider.max = String(roadKmState.totalLengthKm);
        roadKmEnableCheckbox.checked = true;
        applyRoadKmActivation();
        if (!ghostPointEnabled) ghostPointToggle.click();
        const slider = document.getElementById('roadKmSlider');
        slider.value = String(Number(slider.max) * 0.5);
        slider.dispatchEvent(new Event('input', {bubbles: true}));
        await new Promise(resolve => setTimeout(resolve, 80));
        return {wgs, msk, wrong, correct, zone, ghost: modeInput1.value};
    }""")
    assert result["wgs"].startswith("47.200000, 39.800000")
    assert result["msk"]
    assert "(" not in result["msk"]
    assert result["wrong"] == ""
    assert result["correct"] == result["msk"]
    assert result["ghost"].startswith("47.200000, 39.800000 (")
    assert result["ghost"].endswith(")")


def test_km_step_ticks_keyboard_and_wheel_settings(page, app_server):
    login(page, app_server["base_url"])
    page.evaluate("applyRoadKmStep(50, false)")
    page.evaluate("""async () => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        apiFetch = async () => ok({point: {lat: 47.2, lon: 39.8}, road: null, side: null});
        roadKmTitle.textContent = 'М-4';
        roadKmState = buildRoadKmState({
            first_km: 0, last_km: 20,
            sections: [{start_km: 0, finish_km: 20,
                        coordinates: [[39.70, 47.20], [39.90, 47.20]]}]
        });
        roadKmSlider.max = String(roadKmState.totalLengthKm);
        roadKmEnableCheckbox.checked = true;
        applyRoadKmActivation();
        commitPoint(47.2, 39.8, 'click');
        await new Promise(resolve => setTimeout(resolve, 80));
    }""")
    assert page.locator("#roadKmStepButton").inner_text() == "50"
    assert page.locator("#roadKmSlider").get_attribute("step") == "0.05"
    assert page.locator("#roadKmScale").evaluate("el => el.classList.contains('visible')") is True
    page.click("#roadKmStepButton")
    assert page.locator("#roadKmStepButton").inner_text() == "100"
    assert page.locator("#roadKmSlider").get_attribute("step") == "0.1"
    # Настройки используют тот же шаг.
    page.evaluate("""() => {
        const select = document.getElementById('roadKmStepSelect');
        select.value = '500';
        select.dispatchEvent(new Event('change', {bubbles: true}));
    }""")
    assert page.locator("#roadKmStepButton").inner_text() == "500"
    assert page.locator("#roadKmSlider").get_attribute("step") == "0.5"
    # Фокус на ползунке и Tab-доступ включают клавиатуру.
    page.focus("#roadKmSlider")
    before = page.evaluate("Number(roadKmSlider.value)")
    page.keyboard.press("ArrowRight")
    assert page.evaluate("Number(roadKmSlider.value)") > before
    page.evaluate("""() => {
        const toggle = document.getElementById('roadKmKeyboardToggle');
        toggle.checked = false;
        toggle.dispatchEvent(new Event('change', {bubbles: true}));
        roadKmSlider.focus();
    }""")
    before = page.evaluate("Number(roadKmSlider.value)")
    page.keyboard.press("ArrowRight")
    assert page.evaluate("Number(roadKmSlider.value)") == pytest.approx(before)
    # Колесо мыши работает при фокусе в поле км/на ползунке.
    page.evaluate("""() => {
        const key = document.getElementById('roadKmKeyboardToggle');
        key.checked = true; key.dispatchEvent(new Event('change', {bubbles: true}));
        roadKmNumber.focus();
    }""")
    before = page.evaluate("Number(roadKmSlider.value)")
    page.evaluate("document.getElementById('roadKmNumber').dispatchEvent(new WheelEvent('wheel', {deltaY: -100, bubbles: true, cancelable: true}))")
    assert page.evaluate("Number(roadKmSlider.value)") > before
    page.evaluate("""() => {
        const wheel = document.getElementById('roadKmWheelToggle');
        wheel.checked = false; wheel.dispatchEvent(new Event('change', {bubbles: true}));
        roadKmNumber.focus();
    }""")
    before = page.evaluate("Number(roadKmSlider.value)")
    page.evaluate("document.getElementById('roadKmNumber').dispatchEvent(new WheelEvent('wheel', {deltaY: -100, bubbles: true, cancelable: true}))")
    assert page.evaluate("Number(roadKmSlider.value)") == pytest.approx(before)


def test_skdf_name_moves_to_status_bar_left(page, app_server):
    login(page, app_server["base_url"])
    result = page.evaluate("""async () => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
        apiFetch = async (url, options) => {
            if (url === '/api/locate') return ok({
                point: {lat: 47.2, lon: 39.7},
                road: {
                    skdf_name: 'М-4', full_name: 'М-4 «Дон»', source: 'СКДФ WFS',
                    road_code: 'М-4', start_km: 0, finish_km: 20,
                },
                side: {
                    side: 1, km_estimate: 10, perpendicular_m: 12,
                    projected_lat: 47.2, projected_lon: 39.7,
                    km_provenance: 'официальный километраж',
                },
            });
            if (url === '/api/history' && options && options.method === 'POST') return ok({});
            if (url === '/api/geocode/reverse') return ok({});
            return ok({});
        };
        commitPoint(47.2, 39.7, 'click');
        await sleep(120);
        const span = document.getElementById('statusSkdfName');
        const beforeClear = {
            oldElement: Boolean(document.getElementById('resultSkdfName')),
            directoryName: document.getElementById('resultRoadName').textContent,
            statusName: span.textContent,
            statusHidden: span.hidden,
        };
        clearSelectedPoint(false);
        return {
            beforeClear,
            afterClear: {text: span.textContent, hidden: span.hidden},
        };
    }""")
    assert result["beforeClear"]["oldElement"] is False
    assert result["beforeClear"]["directoryName"] == "М-4 «Дон»"
    assert result["beforeClear"]["statusName"] == "М-4"
    assert result["beforeClear"]["statusHidden"] is False
    assert result["afterClear"] == {"text": "", "hidden": True}


def test_current_point_card_shows_active_ghost(page, app_server):
    login(page, app_server["base_url"])
    result = page.evaluate("""async () => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
        const originalFetch = apiFetch;
        apiFetch = async (url, options) => {
            if (url === '/api/locate') return ok({point: {lat: 47.2, lon: 39.7}, road: null, side: null});
            if (url === '/api/msk61/from-geographic') return ok({x: 100, y: 200, zone: 2});
            if (url === '/api/history' && options && options.method === 'POST') return ok({});
            if (url === '/api/geocode/reverse') return ok({});
            return originalFetch(url, options);
        };
        roadKmTitle.textContent = 'М-4';
        roadKmState = buildRoadKmState({
            first_km: 0, last_km: 20,
            sections: [{start_km: 0, finish_km: 20,
                        coordinates: [[39.70, 47.20], [39.90, 47.20]]}]
        });
        roadKmSlider.max = String(roadKmState.totalLengthKm);
        roadKmEnableCheckbox.checked = true;
        applyRoadKmActivation();
        commitPoint(47.2, 39.70, 'click');
        await sleep(80);
        if (!ghostPointEnabled) ghostPointToggle.click();
        const slider = document.getElementById('roadKmSlider');
        slider.value = String(Number(slider.max) * 0.5);
        slider.dispatchEvent(new Event('input', {bubbles: true}));
        await sleep(250);
        const ghostHint = document.getElementById('resultGhostHint');
        const ghostCard = {
            wgs: document.querySelector('#cpWgs .point-value-primary')?.textContent,
            ghostLine: document.querySelector('#cpWgs .point-ghost-line'),
            msk: document.querySelector('#cpMsk .point-value-primary')?.textContent,
            source: document.getElementById('cpSource').textContent,
            favoriteDisabled: document.getElementById('saveFavoriteBtn').disabled,
            hint: ghostHint.hidden ? null : ghostHint.textContent,
            active: [activePoint.lat, activePoint.lon],
            ghost: [ghostLat, ghostLon],
        };
        ghostPointToggle.click();
        await sleep(500);
        const center = map.getCenter();
        return {
            ghostCard,
            afterOff: {
                wgs: document.querySelector('#cpWgs .point-value-primary')?.textContent,
                hintHidden: ghostHint.hidden,
                center: [center.lat, center.lng],
                source: document.getElementById('cpSource').textContent,
            },
        };
    }""")
    assert result["ghostCard"]["wgs"] == "47.200000, 39.700000"
    assert result["ghostCard"]["ghostLine"] is None
    assert result["ghostCard"]["msk"]
    assert "Точка выбрана" in result["ghostCard"]["source"]
    assert result["ghostCard"]["favoriteDisabled"] is False
    assert result["ghostCard"]["hint"].startswith("👻 Призрак")
    assert "47.200000, 39.800000" in result["ghostCard"]["hint"]
    assert result["ghostCard"]["active"] == pytest.approx([47.2, 39.7])
    assert result["ghostCard"]["ghost"] == pytest.approx([47.2, 39.8])
    assert result["afterOff"]["wgs"] == "47.200000, 39.700000"
    assert result["afterOff"]["hintHidden"] is True
    assert result["afterOff"]["center"] == pytest.approx([47.2, 39.7], abs=0.01)
    assert "Точка выбрана" in result["afterOff"]["source"]


def test_ghost_resets_to_point_after_toggle(page, app_server):
    login(page, app_server["base_url"])
    result = page.evaluate("""async () => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        apiFetch = async () => ok({point: {lat: 47.2, lon: 39.8}, road: null, side: null});
        roadKmTitle.textContent = 'М-4';
        roadKmState = buildRoadKmState({
            first_km: 0, last_km: 20,
            sections: [{start_km: 0, finish_km: 20,
                        coordinates: [[39.70, 47.20], [39.90, 47.20]]}]
        });
        roadKmSlider.max = String(roadKmState.totalLengthKm);
        roadKmEnableCheckbox.checked = true;
        applyRoadKmActivation();
        commitPoint(47.2, 39.8, 'click');
        await new Promise(resolve => setTimeout(resolve, 80));
        if (!ghostPointEnabled) ghostPointToggle.click();
        const slider = document.getElementById('roadKmSlider');
        slider.value = String(Number(slider.max) * 0.3);
        slider.dispatchEvent(new Event('input', {bubbles: true}));
        const moved = [ghostLat, ghostLon];
        ghostPointToggle.click();           // выключить
        const hidden = ghostMarker;
        ghostPointToggle.click();           // включить снова
        return {
            moved,
            hidden,
            reset: [ghostLat, ghostLon],
            point: [activePoint.lat, activePoint.lon]
        };
    }""")
    assert result["hidden"] is None
    assert abs(result["moved"][0] - result["reset"][0]) + abs(result["moved"][1] - result["reset"][1]) > 1e-6
    assert result["reset"] == pytest.approx(result["point"], abs=1e-9)


def test_online_road_selection_prepares_point_without_km(page, app_server):
    login(page, app_server["base_url"])
    result = page.evaluate("""async () => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
        const originalFetch = apiFetch;
        apiFetch = async (url, options) => {
            if (url === '/api/roads/online/search') {
                return ok({roads: [{title: 'М-4 Дон', road_code: 'М-4', lat: 47.21, lon: 39.71, precision: 'exact'}]});
            }
            if (url === '/api/roads/online/sections') {
                return ok({first_km: 0, last_km: 20, sections: [{start_km: 0, finish_km: 20,
                    coordinates: [[39.70, 47.20], [39.90, 47.20]]}]});
            }
            if (url === '/api/locate') {
                return ok({point: {lat: 47.2, lon: 39.72}, road: null, side: null});
            }
            if (url === '/api/history' && options && options.method === 'POST') return ok({});
            return originalFetch(url, options);
        };
        setSearchMode('road');
        searchArmCheckbox.checked = true;
        applySearchArmState();
        await resolveOnlineRoad({query: 'М-4'});
        document.querySelector('#modeSuggestions .geo-suggestion').click();
        await sleep(100);
        const prepared = {
            kmEnabled: roadKmEnabled,
            draftLat: searchDraft.roadLat,
            draftKm: searchDraft.roadKm,
        };
        await runSearchAction();
        await sleep(100);
        return {
            prepared,
            active: activePoint ? {lat: activePoint.lat, lon: activePoint.lon, source: activePoint.source} : null,
            searchArmed: searchArmCheckbox.checked,
        };
    }""")
    assert result["prepared"]["kmEnabled"] is False
    assert result["prepared"]["draftLat"] == pytest.approx(47.2, abs=1e-6)
    assert result["prepared"]["draftKm"] is not None
    assert result["active"] is not None
    assert result["active"]["lat"] == pytest.approx(47.2, abs=1e-6)
    assert result["active"]["source"] == "road-km"
    assert result["searchArmed"] is False


def test_road_without_skdf_sections_uses_approximate_osm_axis(page, app_server):
    login(page, app_server["base_url"])
    result = page.evaluate("""async () => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        const missing = data => new Response(JSON.stringify(data), {
            status: 404, headers: {'Content-Type': 'application/json'}
        });
        const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
        const originalFetch = apiFetch;
        const sections = [
            {start_km: 0, finish_km: 10, coordinates: [[39.70, 47.20], [39.80, 47.20]]},
            {start_km: 10, finish_km: 20, coordinates: [[39.80, 47.20], [39.90, 47.20]]},
        ];
        apiFetch = async (url, options) => {
            if (url === '/api/roads/online/search') {
                return ok({roads: [{title: '60К-31 Шахты — Владимировская', road_code: '60К-31',
                                    lat: 47.2, lon: 39.75, precision: 'exact'}]});
            }
            if (url === '/api/roads/online/sections') {
                return missing({detail: 'Для дороги 60К-31 в СКДФ нет участков с официальным километражом'});
            }
            if (url === '/api/roads/overpass/geometry') {
                return ok({
                    code: '60К-31', source: 'OpenStreetMap Overpass', approximate: true,
                    provenance: 'Приблизительная ось по OSM',
                    candidates: [{
                        name: 'Шахты — Владимировская', highway: 'primary',
                        total_length_km: 20, first_km: 0, last_km: 20, way_count: 2,
                        start: [39.7, 47.2], end: [39.9, 47.2], sections,
                    }],
                });
            }
            if (url === '/api/locate') {
                return ok({point: {lat: 47.2, lon: 39.8}, road: null, side: null});
            }
            if (url === '/api/history' && options && options.method === 'POST') return ok({});
            if (url === '/api/geocode/reverse') return ok({});
            return originalFetch(url, options);
        };
        roadKmEnableCheckbox.checked = true;
        applyRoadKmActivation();
        setSearchMode('road');
        searchArmCheckbox.checked = true;
        applySearchArmState();
        await resolveOnlineRoad({query: '60К-31'});
        document.querySelector('#modeSuggestions .geo-suggestion').click();
        for (let i = 0; i < 40 && !roadKmState; i += 1) {
            await sleep(50);
        }
        const prepared = {
            approximate: Boolean(roadKmState && roadKmState.approximate),
            sliderMax: Number(roadKmSlider.max),
            range: roadKmRange.textContent,
            status: roadKmStatus.textContent,
            lineCount: approximateRoadLayer ? approximateRoadLayer.getLayers().length : 0,
        };
        const line = approximateRoadLayer && approximateRoadLayer.getLayers()[0];
        if (line) line.fire('click', {latlng: L.latLng(47.2, 39.8)});
        await sleep(120);
        return {
            prepared,
            point: activePoint ? [activePoint.lat, activePoint.lon] : null,
            km: roadKmNumber.value,
            source: activePoint ? activePoint.source : null,
        };
    }""")
    assert result["prepared"]["approximate"] is True
    assert result["prepared"]["sliderMax"] == pytest.approx(20)
    assert "Приблизительная" in result["prepared"]["range"]
    assert "Официальных участков СКДФ нет" in result["prepared"]["status"]
    assert result["prepared"]["lineCount"] >= 3  # две линии + маркеры начала/конца
    assert result["point"] == pytest.approx([47.2, 39.8])
    assert result["km"]
    assert result["source"] == "road-km"


def test_clear_point_clears_search_draft_and_ghost_state(page, app_server):
    login(page, app_server["base_url"])
    result = page.evaluate("""async () => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
        apiFetch = async () => ok({point: {lat: 47.3, lon: 39.9}, road: null, side: null});
        setSearchMode('address');
        searchArmCheckbox.checked = true;
        applySearchArmState();
        searchDraft.addressLat = 47.3;
        searchDraft.addressLon = 39.9;
        searchDraft.addressLabel = 'Старый адрес';
        modeInput1.value = 'Старый адрес';
        commitPoint(47.3, 39.9, 'address', {address: 'Старый адрес'});
        await sleep(80);
        if (!ghostPointEnabled) ghostPointToggle.click();
        clearSelectedPoint(false);
        return {
            field: modeInput1.value,
            draftLat: searchDraft.addressLat,
            draftLon: searchDraft.addressLon,
            active: Boolean(activePoint),
            ghostPressed: ghostPointToggle.getAttribute('aria-pressed'),
            ghostFlag: ghostPointEnabled,
        };
    }""")
    assert result["field"] == ""
    assert result["draftLat"] is None
    assert result["draftLon"] is None
    assert result["active"] is False
    assert result["ghostPressed"] == "false"
    assert result["ghostFlag"] is False


def test_batch_slider_updates_batch_point_and_marker(page, app_server):
    login(page, app_server["base_url"])
    result = page.evaluate("""async () => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
        apiFetch = async () => ok({point: {lat: 47.21, lon: 39.71}, road: null, side: null});
        batchPoints = [{id: 'bp-sync', name: 'Тест', filename: '', lat: 47.21, lon: 39.71}];
        renderBatchList();
        roadKmTitle.textContent = 'М-4';
        roadKmState = buildRoadKmState({
            first_km: 0, last_km: 20,
            sections: [{start_km: 0, finish_km: 20,
                        coordinates: [[39.70, 47.20], [39.90, 47.20]]}]
        });
        roadKmSlider.max = String(roadKmState.totalLengthKm);
        roadKmEnableCheckbox.checked = true;
        applyRoadKmActivation();
        selectBatchPoint('bp-sync');
        await sleep(60);
        const slider = document.getElementById('roadKmSlider');
        slider.value = String(Number(slider.max) * 0.5);
        slider.dispatchEvent(new Event('input', {bubbles: true}));
        await sleep(60);
        const point = batchPoints[0];
        const marker = batchMarker(point);
        const row = document.querySelector('.batch-item[data-id="bp-sync"] .batch-status-ok');
        return {
            batch: [point.lat, point.lon],
            active: [activePoint.lat, activePoint.lon],
            marker: marker ? [marker.getLatLng().lat, marker.getLatLng().lng] : null,
            rowText: row ? row.textContent : '',
        };
    }""")
    assert result["batch"] == pytest.approx(result["active"], abs=1e-9)
    assert result["marker"] == pytest.approx(result["active"], abs=1e-9)
    assert result["rowText"].startswith(f"{result['batch'][0]:.5f}, {result['batch'][1]:.5f}")


def test_batch_photo_lock_is_released_by_clearing_batch_list(page, app_server):
    login(page, app_server["base_url"])
    result = page.evaluate("""async () => {
        batchPoints = [{id: 'bp-lock', name: 'Тест', filename: '', lat: 47.2, lon: 39.8}];
        markPhotosLoaded('batch');
        const disabledAfterLoad = searchArmCheckbox.disabled;
        clearBatchPoints(true);
        await new Promise(resolve => setTimeout(resolve, 50));
        return {
            disabledAfterLoad,
            disabledAfterClear: searchArmCheckbox.disabled,
            photosLoaded,
            clearButtonHidden: document.getElementById('clearPointBtn').style.display === 'none',
        };
    }""")
    assert result["disabledAfterLoad"] is True
    assert result["disabledAfterClear"] is False
    assert result["photosLoaded"] is False
    assert result["clearButtonHidden"] is True


def test_msk_place_button_uses_commit_point(page, app_server):
    login(page, app_server["base_url"])
    result = page.evaluate("""async () => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
        apiFetch = async () => ok({point: {lat: 47.25, lon: 39.75}, road: null, side: null});
        activePoint = null;
        selectedMskPoint = {lat: 47.25, lon: 39.75, can_place_on_map: true, warning: 'x'};
        window.confirm = () => true;
        placeMSKPoint();
        await sleep(80);
        return {
            active: activePoint ? {lat: activePoint.lat, lon: activePoint.lon, source: activePoint.source} : null,
            cardEmpty: !document.getElementById('currentPointEmpty').hidden,
        };
    }""")
    assert result["active"] is not None
    assert result["active"]["lat"] == pytest.approx(47.25)
    assert result["active"]["source"] == "msk61"
    assert result["cardEmpty"] is False


def test_track_point_toggle_follows_slider_and_ghost(page, app_server):
    login(page, app_server["base_url"])
    page.wait_for_timeout(250)
    result = page.evaluate("""async () => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
        apiFetch = async () => ok({point: {lat: 47.2, lon: 39.7}, road: null, side: null});
        roadKmTitle.textContent = 'М-4';
        roadKmState = buildRoadKmState({
            first_km: 0, last_km: 20,
            sections: [{start_km: 0, finish_km: 20,
                        coordinates: [[39.70, 47.20], [39.90, 47.20]]}]
        });
        roadKmSlider.max = String(roadKmState.totalLengthKm);
        roadKmEnableCheckbox.checked = true;
        applyRoadKmActivation();
        commitPoint(47.2, 39.70, 'click');
        await sleep(100);
        map.setView([47.2, 39.70], 16, {animate: false});
        await sleep(200);

        // Включение кнопкой в статус-баре синхронизирует чекбокс в настройках.
        trackPointToggle.click();
        const controlsOn = {
            setting: trackPointSetting.checked,
            aria: trackPointToggle.getAttribute('aria-pressed'),
        };

        const slider = document.getElementById('roadKmSlider');
        slider.value = slider.max;
        slider.dispatchEvent(new Event('input', {bubbles: true}));
        await sleep(250);
        const centerAfterTrack = map.getCenter();
        const activeTracked = map.getBounds().contains([activePoint.lat, activePoint.lon]);
        const keyboardDuring = map.keyboard.enabled();

        // Призрак двигается вместе с ползунком, красная точка стоит.
        if (!ghostPointEnabled) ghostPointToggle.click();
        map.setView([47.2, 39.70], 16, {animate: false});
        await sleep(200);
        slider.dispatchEvent(new Event('input', {bubbles: true}));
        await sleep(250);
        const ghostTracked = map.getBounds().contains([ghostLat, ghostLon]);
        const redPoint = [activePoint.lat, activePoint.lon];

        // Выключение через настройки: карта больше не следует за ползунком,
        // клавиатурное управление картой возвращается.
        trackPointSetting.checked = false;
        trackPointSetting.dispatchEvent(new Event('change', {bubbles: true}));
        const ariaOff = trackPointToggle.getAttribute('aria-pressed');
        const keyboardAfter = map.keyboard.enabled();
        map.setView([47.2, 39.85], 16, {animate: false});
        await sleep(200);
        const centerBefore = map.getCenter();
        slider.value = '0';
        slider.dispatchEvent(new Event('input', {bubbles: true}));
        await sleep(250);
        const centerAfter = map.getCenter();
        applyTrackPointState(false, false);
        try { localStorage.removeItem('albion_track_point'); } catch (e) { /* ignore */ }
        return {
            controlsOn, activeTracked, ghostTracked, redPoint,
            ariaOff, keyboardDuring, keyboardAfter,
            centerAfterTrack: [centerAfterTrack.lat, centerAfterTrack.lng],
            centerBefore: [centerBefore.lat, centerBefore.lng],
            centerAfter: [centerAfter.lat, centerAfter.lng],
        };
    }""")
    assert result["controlsOn"] == {"setting": True, "aria": "true"}
    assert result["activeTracked"] is True
    assert result["centerAfterTrack"][1] == pytest.approx(39.9, abs=1e-4)
    assert result["ghostTracked"] is True
    assert result["redPoint"][1] > 39.85
    assert result["ariaOff"] == "false"
    assert result["keyboardDuring"] is False
    assert result["keyboardAfter"] is True
    assert result["centerAfter"][1] == pytest.approx(result["centerBefore"][1], abs=0.002)


def test_go_button_centers_map_on_selected_km(page, app_server):
    login(page, app_server["base_url"])
    result = page.evaluate("""async () => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
        let historyPosts = 0;
        apiFetch = async (url, options) => {
            if (url === '/api/history' && options && options.method === 'POST') {
                historyPosts += 1;
                return ok({});
            }
            return ok({point: {lat: 47.2, lon: 39.7}, road: null, side: null});
        };
        applyTrackPointState(false, false);
        roadKmTitle.textContent = 'М-4';
        roadKmState = buildRoadKmState({
            first_km: 0, last_km: 20,
            sections: [{start_km: 0, finish_km: 20,
                        coordinates: [[39.70, 47.20], [39.90, 47.20]]}]
        });
        roadKmSlider.max = String(roadKmState.totalLengthKm);
        roadKmEnableCheckbox.checked = true;
        applyRoadKmActivation();
        commitPoint(47.2, 39.70, 'click');
        await sleep(100);
        const historyBefore = historyPosts;
        map.setView([47.2, 39.95], 10, {animate: false});
        roadKmNumber.value = '10+000';
        roadKmApplyButton.click();
        await sleep(100);
        const center = map.getCenter();
        applyTrackPointState(false, false);
        return {
            center: [center.lat, center.lng],
            zoom: map.getZoom(),
            point: [activePoint.lat, activePoint.lon],
            historyAdded: historyPosts - historyBefore,
        };
    }""")
    assert result["center"][1] == pytest.approx(39.8, abs=1e-4)
    assert result["zoom"] >= 16
    assert result["point"][1] == pytest.approx(39.8, abs=1e-4)
    assert result["historyAdded"] == 1


def test_arrow_keys_require_text_focus_or_marker_click(page, app_server):
    login(page, app_server["base_url"])
    page.evaluate("""async () => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        apiFetch = async () => ok({point: {lat: 47.2, lon: 39.8}, road: null, side: null});
        roadKmTitle.textContent = 'М-4';
        roadKmState = buildRoadKmState({
            first_km: 0, last_km: 20,
            sections: [{start_km: 0, finish_km: 20,
                        coordinates: [[39.70, 47.20], [39.90, 47.20]]}]
        });
        roadKmSlider.max = String(roadKmState.totalLengthKm);
        roadKmEnableCheckbox.checked = true;
        applyRoadKmActivation();
        commitPoint(47.2, 39.8, 'click');
        await new Promise(resolve => setTimeout(resolve, 80));
        document.activeElement && document.activeElement.blur();
    }""")
    before = page.evaluate("[Number(roadKmSlider.value), activePoint.lat, activePoint.lon]")
    page.keyboard.press("ArrowRight")
    unarmed = page.evaluate("Number(roadKmSlider.value)")
    assert unarmed == pytest.approx(before[0])
    page.focus("#roadKmNumber")
    page.keyboard.press("ArrowRight")
    focused = page.evaluate("[Number(roadKmSlider.value), activePoint.lat, activePoint.lon]")
    assert focused[0] > before[0]
    # Ghost mode: click on the green clone arms arrows and moves only the clone.
    page.evaluate("""() => {
        if (!ghostPointEnabled) toggleGhostPoint();
        const slider = document.getElementById('roadKmSlider');
        slider.value = String(Number(slider.max) * 0.2);
        slider.dispatchEvent(new Event('input', {bubbles: true}));
        document.activeElement && document.activeElement.blur();
        ghostMarker.fire('click');
    }""")
    original = page.evaluate("[activePoint.lat, activePoint.lon, ghostLat, ghostLon]")
    page.keyboard.press("ArrowRight")
    ghost_moved = page.evaluate("[activePoint.lat, activePoint.lon, ghostLat, ghostLon]")
    assert ghost_moved[:2] == pytest.approx(original[:2])
    assert abs(ghost_moved[2] - original[2]) + abs(ghost_moved[3] - original[3]) > 1e-7
    # Without ghost, clicking the red marker also arms the arrows.
    page.evaluate("""() => {
        if (ghostPointEnabled) toggleGhostPoint();
        document.activeElement && document.activeElement.blur();
        redPointMarker.fire('click');
    }""")
    red_before = page.evaluate("[activePoint.lat, activePoint.lon]")
    page.keyboard.press("ArrowLeft")
    red_after = page.evaluate("[activePoint.lat, activePoint.lon]")
    assert abs(red_after[0] - red_before[0]) + abs(red_after[1] - red_before[1]) > 1e-7


def test_map_double_click_sets_point_without_changing_view(page, app_server):
    login(page, app_server["base_url"])
    page.wait_for_timeout(300)
    page.evaluate("""() => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        apiFetch = async (url) => {
            if (url === '/api/locate') {
                return ok({point: {lat: 48.5, lon: 41.5}, road: null, side: null});
            }
            if (url === '/api/msk61/from-geographic') {
                return ok({x: 422630.864, y: 2204440.047, zone: 2,
                           profile: 'gost_51794_2008', warning: ''});
            }
            return ok({});
        };
        map.setView([47.0, 40.0], 8, {animate: false});
    }""")
    before = page.evaluate(
        "({lat: map.getCenter().lat, lon: map.getCenter().lng, zoom: map.getZoom()})"
    )
    page.evaluate("map.fire('dblclick', {latlng: L.latLng(48.5, 41.5)})")
    page.wait_for_function("activePoint && Math.abs(activePoint.lat - 48.5) < 1e-6")
    after = page.evaluate(
        "({lat: map.getCenter().lat, lon: map.getCenter().lng, zoom: map.getZoom()})"
    )
    assert after["zoom"] == before["zoom"]
    assert after["lat"] == pytest.approx(before["lat"], abs=1e-6)
    assert after["lon"] == pytest.approx(before["lon"], abs=1e-6)
    assert not page._e2e_errors, page._e2e_errors


def test_detailed_boundary_falls_back_to_local_file(page, app_server):
    login(page, app_server["base_url"])
    page.route(
        "**/static/rostov_boundary_detailed.geojson",
        lambda route: route.fulfill(status=404, body="not found"),
    )
    page.evaluate("""async () => {
        screenshotSettings.detailedBoundary = true;
        await loadRostovBoundary();
    }""")
    state = page.evaluate(
        "({line: !!boundaryLineLayer, mask: !!boundaryMaskLayer,"
        " status: document.getElementById('folderStatus').textContent})"
    )
    assert state["line"] and state["mask"], state
    assert "696" in state["status"], state
    assert not page._e2e_errors, page._e2e_errors


def test_batch_mode_uses_selected_point_and_blocks_new_search_points(page, app_server):
    login(page, app_server["base_url"])
    page.evaluate("""() => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        apiFetch = async () => ok({point: {lat: 47.5, lon: 40.0}, road: null, side: null});
        activateFrameTab('batch');
        batchPoints = [{id: 'b1', name: 'Точка 1', lat: 47.5, lon: 40.0}];
        renderBatchList();
    }""")
    assert page.locator("#searchArmCheckbox").is_disabled()
    assert page.locator("#tabBatchBtn").get_attribute("aria-selected") == "true"
    page.click(".batch-item")
    page.wait_for_function("activePoint && activePoint.source === 'batch'")
    before = page.evaluate("[activePoint.lat, activePoint.lon]")
    page.evaluate("map.fire('dblclick', {latlng: L.latLng(48.5, 41.5)})")
    after = page.evaluate("[activePoint.lat, activePoint.lon]")
    assert after == pytest.approx(before)
    assert page.locator("#statusPointText").inner_text() != ""
    page.click("#tabPointBtn")
    assert page.locator("#searchArmCheckbox").is_enabled()


def test_search_mode_locks_photo_and_batch_and_clear_unlocks(page, app_server):
    login(page, app_server["base_url"])
    page.check("#searchArmCheckbox")
    assert page.locator("#tabBatchBtn").is_disabled()
    assert page.locator("#fileInput").is_disabled()
    assert page.locator("#batchFileInput").is_disabled()
    assert "disabled" in page.locator("#dropZone").get_attribute("class")
    page.uncheck("#searchArmCheckbox")
    assert page.locator("#tabBatchBtn").is_enabled()
    assert page.locator("#fileInput").is_enabled()
    locked = page.evaluate("""() => {
        photosLoaded = true;
        updateModeLocks();
        return {
            searchDisabled: searchArmCheckbox.disabled,
            photos: photosLoaded
        };
    }""")
    assert locked == {"searchDisabled": True, "photos": True}
    page.evaluate("clearSelectedPoint()")
    assert page.locator("#searchArmCheckbox").is_enabled()


def test_road_suggestions_appear_while_typing(page, app_server):
    login(page, app_server["base_url"])
    page.evaluate("""() => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        apiFetch = async () => ok({roads: [{
            title: 'М-4 Дон', road_code: 'М-4', precision: 'exact',
            lat: 47.2, lon: 39.8, query: 'М-4'
        }]});
        document.getElementById('searchArmCheckbox').checked = true;
        applySearchArmState();
        document.getElementById('modeRoadBtn').click();
        const input = document.getElementById('modeInput1');
        input.value = 'М-4';
        input.dispatchEvent(new Event('input', {bubbles: true}));
    }""")
    page.wait_for_selector("#modeSuggestions.open .geo-suggestion", timeout=5000)
    assert page.locator("#modeSuggestions .geo-suggestion").count() >= 1


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
        roadKmEnableCheckbox.checked = true;
        applyRoadKmActivation();
        searchDraft.mode = 'road';
        const preview = previewRoadKm(10);
        const gapStatus = roadKmStatus.textContent;
        const outside = previewRoadKm(999);
        const outsideStatus = roadKmStatus.textContent;
        return {
            gaps: gapState.gaps,
            hasGaps: gapState.hasGaps,
            intervals: gapState.intervals,
            hasOverlaps: overlaps.hasOverlaps,
            overlapIntervals: overlaps.intervals,
            preview,
            draftLat: searchDraft.roadLat,
            modeInput: modeInput2.value,
            status: gapStatus,
            outside,
            outsideStatus,
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
    assert result["outside"] is None
    assert "вне доступного диапазона" in result["outsideStatus"]


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
        roadKmEnableCheckbox.checked = true;
        applyRoadKmActivation();
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


def test_map_point_auto_loads_official_sections(page, app_server):
    login(page, app_server["base_url"])
    result = page.evaluate("""async () => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        apiFetch = async (url) => {
            if (url === '/api/locate') return ok({
                point: {lat: 47.2, lon: 39.8, bearing: null},
                road: {road_code: 'М-4', full_name: 'М-4 Дон', skdf_name: 'М-4'},
                side: null
            });
            if (url === '/api/roads/online/sections') return ok({
                code: 'М-4', name: 'М-4', first_km: 0, last_km: 20,
                sections: [{
                    start_km: 0, finish_km: 20,
                    coordinates: [[39.70, 47.20], [39.90, 47.20]]
                }]
            });
            if (url === '/api/history') return ok({status: 'ok', entries: []});
            if (url === '/api/geocode/reverse') return ok({address: null, district: null});
            return ok({status: 'ok'});
        };
        commitPoint(47.2, 39.8, 'click');
        for (let i = 0; i < 60 && !roadKmState; i += 1) {
            await new Promise(resolve => setTimeout(resolve, 50));
        }
        const inactive = {
            disabled: roadKmSlider.disabled,
            km: roadKmNumber.value,
            status: roadKmStatus.textContent
        };
        roadKmEnableCheckbox.checked = true;
        applyRoadKmActivation();
        return {
            code: roadKmState ? roadKmState.code : null,
            title: roadKmTitle.textContent,
            inactive,
            active: {
                disabled: roadKmSlider.disabled,
                km: roadKmNumber.value,
                status: roadKmStatus.textContent,
                range: roadKmRange.textContent
            }
        };
    }""")
    assert result["code"] == "М-4"
    assert "М-4" in result["title"]
    assert result["inactive"]["disabled"] is True
    assert result["inactive"]["km"] == ""
    assert result["inactive"]["status"] == ""
    assert result["active"]["disabled"] is False
    assert result["active"]["km"]
    assert ("выбранной точке" in result["active"]["status"]
            or "синхронизирован" in result["active"]["status"])


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
        roadKmEnableCheckbox.checked = true;
        applyRoadKmActivation();
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


def test_km_change_and_enter_commit_only_once(page, app_server):
    """Enter, а затем blur/change того же значения не должны дублировать запись."""
    login(page, app_server["base_url"])
    result = page.evaluate("""() => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        let historyPosts = 0;
        apiFetch = async (url, options) => {
            if (url.startsWith('/api/history') && options && options.method === 'POST') {
                historyPosts += 1;
            }
            return ok({entries: []});
        };
        activePoint = {lat: 47.2, lon: 39.8, source: 'click'};
        roadKmTitle.textContent = 'М-4';
        roadKmState = buildRoadKmState({
            first_km: 0, last_km: 20,
            sections: [{
                start_km: 0, finish_km: 20,
                coordinates: [[39.70, 47.20], [39.90, 47.20]]
            }]
        });
        roadKmSlider.max = String(roadKmState.totalLengthKm);
        roadKmEnableCheckbox.checked = true;
        applyRoadKmActivation();
        roadKmNumber.value = '10+000';
        roadKmNumber.dispatchEvent(new Event('input', {bubbles: true}));
        roadKmNumber.dispatchEvent(new Event('change', {bubbles: true}));
        const afterChange = historyPosts;
        applyRoadKmNumber(true);  // эквивалент Enter
        return {afterChange, afterEnter: historyPosts};
    }""")
    assert result["afterChange"] == 1
    assert result["afterEnter"] == 1


def test_removing_selected_batch_point_clears_map_selection(page, app_server):
    """Удаление выбранной точки не должно оставлять её в activePoint."""
    login(page, app_server["base_url"])
    result = page.evaluate("""async () => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        apiFetch = async () => ok({point: {lat: 47.2, lon: 39.8}, road: null, side: null});
        batchPoints = [
            {id: 'a', name: 'A', lat: 47.2, lon: 39.8},
            {id: 'b', name: 'B', lat: 47.3, lon: 39.9},
        ];
        renderBatchList();
        selectBatchPoint('a');
        await new Promise(resolve => setTimeout(resolve, 60));
        editBatchPoint('b');
        const before = {
            activeId: batchEdit.activeId,
            selectedId: activePoint ? activePoint.batchId : null,
        };
        removeBatchPoint('a');
        return {
            before,
            remaining: batchPoints.map(point => point.id),
            active: activePoint,
            marker: Boolean(redPointMarker),
        };
    }""")
    assert result["before"] == {"activeId": "b", "selectedId": "a"}
    assert result["remaining"] == ["b"]
    assert result["active"] is None
    assert result["marker"] is False


def test_optional_url_state_sync_shares_and_restores_point(page, app_server):
    """Opt-in режим: состояние попадает в URL и восстанавливается по ссылке."""
    login(page, app_server["base_url"])
    page.click("#settingsOpenButton")
    page.check("#urlStateSyncToggle")
    page.keyboard.press("Escape")
    page.evaluate("() => saveUserState()")
    expected_mode = page.evaluate("searchDraft.mode")
    # Точка ставится через единый commitPoint; locate мокаем.
    page.evaluate("""async () => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        const original = apiFetch;
        apiFetch = async (url, options) => {
            if (url === '/api/locate') return ok({point: {lat: 47.25, lon: 39.75}, road: null, side: null});
            return original(url, options);
        };
        commitPoint(47.25, 39.75, 'click');
        await new Promise(resolve => setTimeout(resolve, 700));
    }""")
    page.wait_for_function("location.hash.startsWith('#s=v1')", timeout=5000)
    compact = page.evaluate("""() => {
        const encoded = new URLSearchParams(location.hash.slice(1)).get('s');
        return {
            encoded,
            state: AlbionGeo.decodeUrlState(encoded),
            search: location.search,
        };
    }""")
    assert compact["encoded"].startswith("v1")
    assert len(compact["encoded"]) <= 40
    assert compact["state"]["point"]["lat"] == pytest.approx(47.25, abs=1e-6)
    assert compact["state"]["point"]["lon"] == pytest.approx(39.75, abs=1e-6)
    assert compact["state"]["mode"] == expected_mode
    assert compact["search"] == ""

    # Новая компактная ссылка в hash восстанавливает точку и режим.
    encoded = page.evaluate(
        "AlbionGeo.encodeUrlState({lat: 47.3, lon: 39.9, zoom: 16, mode: 'coords', tab: 'point'})"
    )
    page.goto(app_server["base_url"] + "/#s=" + encoded)
    page.wait_for_selector("body.authenticated")
    page.wait_for_function(
        "activePoint && Math.abs(activePoint.lat - 47.3) < 1e-6"
        " && Math.abs(activePoint.lon - 39.9) < 1e-6",
        timeout=8000,
    )
    assert page.evaluate("searchDraft.mode") == "coords"
    page.wait_for_function("map.getZoom() === 16", timeout=3000)
    page.wait_for_timeout(300)
    assert page.evaluate("map.getZoom()") == 16
    linked = page.evaluate("""() => {
        const encoded = new URLSearchParams(location.hash.slice(1)).get('s');
        return AlbionGeo.decodeUrlState(encoded);
    }""")
    assert linked["zoom"] == 16

    # Старые длинные ссылки продолжают работать.
    page.goto(app_server["base_url"] + "/?lat=47.4&lon=40.1&zoom=15&mode=road")
    page.wait_for_selector("body.authenticated")
    page.wait_for_function(
        "activePoint && Math.abs(activePoint.lat - 47.4) < 1e-6"
        " && Math.abs(activePoint.lon - 40.1) < 1e-6",
        timeout=8000,
    )

    # Выключаем режим, чтобы настройка не влияла на другие тесты.
    page.evaluate("applyUrlStateSyncSetting(false, true)")
    page.evaluate("() => saveUserState()")


def test_optional_multi_select_bulk_show_and_delete(page, app_server):
    """Opt-in режим: чекбоксы, показ выбранных на карте и массовое удаление."""
    login(page, app_server["base_url"])
    page.click("#settingsOpenButton")
    page.check("#multiSelectToggle")
    page.keyboard.press("Escape")
    page.evaluate("""() => {
        activateFrameTab('batch');
        batchPoints = [
            {id: 'a', name: 'A', lat: 47.2, lon: 39.8},
            {id: 'b', name: 'B', lat: 47.3, lon: 39.9},
            {id: 'c', name: 'C', lat: 47.4, lon: 40.0},
        ];
        renderBatchList();
        updateBatchBulkBar();
    }""")
    assert page.locator(".batch-select").count() == 3
    page.check('.batch-item[data-id="a"] .batch-select')
    page.check('.batch-item[data-id="c"] .batch-select')
    assert page.locator("#batchBulkBar").is_visible()
    assert "2" in page.locator("#batchBulkCount").inner_text()

    page.click("#batchBulkShowBtn")
    assert page.evaluate("batchLayerGroup.getLayers().length") == 2

    page.once("dialog", lambda dialog: dialog.accept())
    page.click("#batchBulkDeleteBtn")
    assert page.evaluate("batchPoints.map(point => point.id)") == ["b"]
    assert page.locator("#batchBulkBar").is_hidden()
    assert page.locator(".batch-select").count() == 1

    page.evaluate("applyMultiSelectSetting(false, true)")
    page.evaluate("() => saveUserState()")


def test_new_point_blocks_stale_km_until_locate(page, app_server):
    """Старый километраж нельзя применить, пока locate не подтвердил дорогу."""
    login(page, app_server["base_url"])
    result = page.evaluate("""async () => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
        let releaseLocate = null;
        let historyPosts = 0;
        apiFetch = async (url, options) => {
            if (url === '/api/locate') {
                const body = JSON.parse((options && options.body) || '{}');
                // Новая точка отвечает с задержкой, старая — сразу.
                if (Math.abs(body.lat - 47.5) < 1e-9) {
                    await new Promise(resolve => { releaseLocate = resolve; });
                    return ok({point: {lat: 47.5, lon: 40.5}, road: null, side: null});
                }
                return ok({point: {lat: 47.2, lon: 39.8}, road: null, side: null});
            }
            if (url.startsWith('/api/history') && options && options.method === 'POST') {
                historyPosts += 1;
            }
            return ok({entries: [], status: 'ok'});
        };
        activePoint = {lat: 47.2, lon: 39.8, source: 'click'};
        roadKmTitle.textContent = 'М-4';
        roadKmState = buildRoadKmState({
            first_km: 0, last_km: 20,
            sections: [{
                start_km: 0, finish_km: 20,
                coordinates: [[39.70, 47.20], [39.90, 47.20]]
            }]
        });
        roadKmSlider.max = String(roadKmState.totalLengthKm);
        roadKmEnableCheckbox.checked = true;
        applyRoadKmActivation();
        commitPoint(47.5, 40.5, 'click');   // locate для неё ждёт releaseLocate
        roadKmNumber.value = '20+000';
        applyRoadKmNumber(true);
        const during = {
            point: [activePoint.lat, activePoint.lon],
            stale: roadKmStale,
            disabled: roadKmSlider.disabled,
            history: historyPosts,
        };
        releaseLocate();
        await sleep(120);
        return {
            during,
            after: {
                point: [activePoint.lat, activePoint.lon],
                stale: roadKmStale,
                disabled: roadKmSlider.disabled,
                hasState: Boolean(roadKmState),
            },
        };
    }""")
    assert result["during"]["point"] == pytest.approx([47.5, 40.5])
    assert result["during"]["stale"] is True
    assert result["during"]["disabled"] is True
    assert result["during"]["history"] == 0
    assert result["after"]["point"] == pytest.approx([47.5, 40.5])
    assert result["after"]["stale"] is False
    assert result["after"]["hasState"] is False


def test_screenshot_rect_can_be_moved_by_middle(page, app_server):
    """После фиксации выделения рамку можно тянуть за середину."""
    login(page, app_server["base_url"])
    page.evaluate("""async () => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        apiFetch = async () => ok({point: {lat: 47.2, lon: 39.8}, road: null, side: null});
        commitPoint(47.2, 39.8, 'click');
        await new Promise(resolve => setTimeout(resolve, 80));
        startScreenshotMode();
    }""")
    before = page.evaluate("""() => ({
        left: parseInt(rect.style.left),
        top: parseInt(rect.style.top),
        pointerEvents: getComputedStyle(rect).pointerEvents,
    })""")
    assert before["pointerEvents"] == "auto"
    box = page.locator("#selection-rect").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2 + 35,
                    box["y"] + box["height"] / 2 + 25, steps=5)
    page.mouse.up()
    after = page.evaluate("""() => ({
        left: parseInt(rect.style.left),
        top: parseInt(rect.style.top),
    })""")
    assert after["left"] == pytest.approx(before["left"] + 35, abs=2)
    assert after["top"] == pytest.approx(before["top"] + 25, abs=2)
    page.evaluate("clearScreenshotUI()")
    assert page.evaluate("rect.classList.contains('editable')") is False


def test_mobile_status_bar_wraps_without_horizontal_overflow(page, app_server):
    """На 390px статус-бар переносится, кнопка настроек остаётся в экране."""
    page.set_viewport_size({"width": 390, "height": 844})
    login(page, app_server["base_url"])
    metrics = page.evaluate("""() => {
        const doc = document.documentElement;
        const bar = document.getElementById('status-bar').getBoundingClientRect();
        const gear = document.getElementById('settingsOpenButton').getBoundingClientRect();
        return {
            scrollWidth: doc.scrollWidth,
            clientWidth: doc.clientWidth,
            barHeight: bar.height,
            gearLeft: gear.left,
            gearRight: gear.right,
            gearVisible: gear.width > 0,
        };
    }""")
    assert metrics["scrollWidth"] <= metrics["clientWidth"] + 1
    assert metrics["barHeight"] >= 54
    assert metrics["gearVisible"] is True
    assert metrics["gearLeft"] >= 0
    assert metrics["gearRight"] <= 391
    page.click("#settingsOpenButton")
    assert page.locator("#settings-modal").evaluate("el => el.classList.contains('open')")


def test_multi_select_checkbox_keyboard_and_shift(page, app_server):
    """Space переключает чекбокс без потери фокуса, Shift по чекбоксу — диапазон."""
    login(page, app_server["base_url"])
    page.click("#settingsOpenButton")
    page.check("#multiSelectToggle")
    page.keyboard.press("Escape")
    page.evaluate("""() => {
        activateFrameTab('batch');
        batchPoints = [
            {id: 'a', name: 'A', lat: 47.2, lon: 39.8},
            {id: 'b', name: 'B', lat: 47.3, lon: 39.9},
            {id: 'c', name: 'C', lat: 47.4, lon: 40.0},
        ];
        renderBatchList();
        updateBatchBulkBar();
    }""")
    checkbox = page.locator('.batch-item[data-id="a"] .batch-select')
    checkbox.focus()
    page.keyboard.press("Space")
    assert checkbox.is_checked()
    assert page.evaluate("batchSelection.size") == 1
    assert page.evaluate("document.activeElement === document.querySelector('.batch-item[data-id=\"a\"] .batch-select')")
    page.keyboard.press("Space")
    assert not checkbox.is_checked()
    assert page.evaluate("batchSelection.size") == 0
    assert page.locator("#batchBulkBar").is_hidden()

    checkbox.click()
    page.locator('.batch-item[data-id="c"] .batch-select').click(modifiers=["Shift"])
    assert sorted(page.evaluate("Array.from(batchSelection)")) == ["a", "b", "c"]
    page.evaluate("applyMultiSelectSetting(false, true)")
    page.evaluate("() => saveUserState()")


def test_escape_closes_suggestions_from_search_field(page, app_server):
    """Esc внутри панели поиска закрывает подсказки и снимает фокус."""
    login(page, app_server["base_url"])
    page.evaluate("""() => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        apiFetch = async () => ok({roads: [{
            title: 'М-4 Дон', road_code: 'М-4', precision: 'exact',
            lat: 47.2, lon: 39.8, query: 'М-4'
        }]});
        document.getElementById('searchArmCheckbox').checked = true;
        applySearchArmState();
        document.getElementById('modeRoadBtn').click();
    }""")
    page.fill("#modeInput1", "М-4")
    page.wait_for_selector("#modeSuggestions.open .geo-suggestion", timeout=5000)
    assert page.evaluate("document.activeElement === modeInput1")
    page.keyboard.press("Escape")
    assert page.evaluate("!document.getElementById('modeSuggestions').classList.contains('open')")
    assert page.evaluate("document.activeElement !== modeInput1")


def test_ghost_keeps_bookmark_button_for_original_point(page, app_server):
    """Левая карточка/закладки в режиме призрака относятся к оригинальной точке."""
    login(page, app_server["base_url"])
    result = page.evaluate("""() => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        apiFetch = async () => ok({x: 422630.864, y: 2204440.047, zone: 2});
        activePoint = {lat: 47.2, lon: 39.8, source: 'click'};
        renderBookmarks();
        const before = document.getElementById('addBookmarkBtn').disabled;
        toggleGhostPoint();
        const during = document.getElementById('addBookmarkBtn').disabled;
        toggleGhostPoint();
        const after = document.getElementById('addBookmarkBtn').disabled;
        removeGhostMarker();
        return {before, during, after};
    }""")
    assert result == {"before": False, "during": False, "after": False}, result


def test_km_range_restored_after_activation(page, app_server):
    """Диапазон и предупреждения восстанавливаются при включении км после выбора дороги."""
    login(page, app_server["base_url"])
    result = page.evaluate("""async () => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
        apiFetch = async (url) => {
            if (url === '/api/roads/online/search') {
                return ok({roads: [{title: 'М-4 Дон', road_code: 'М-4', lat: 47.2, lon: 39.8, precision: 'exact'}]});
            }
            if (url === '/api/roads/online/sections') {
                return ok({code: 'М-4', first_km: 0, last_km: 20, sections: [
                    {start_km: 0, finish_km: 5, coordinates: [[39.70, 47.20], [39.74, 47.20]]},
                    {start_km: 15, finish_km: 20, coordinates: [[39.80, 47.20], [39.84, 47.20]]},
                ]});
            }
            if (url === '/api/locate') return ok({point: {lat: 47.2, lon: 39.8}, road: null, side: null});
            return ok({entries: [], status: 'ok'});
        };
        setSearchMode('road');
        searchArmCheckbox.checked = true;
        applySearchArmState();
        await resolveOnlineRoad({query: 'М-4'});
        document.querySelector('#modeSuggestions .geo-suggestion').click();
        for (let i = 0; i < 40 && !roadKmState; i += 1) await sleep(25);
        const rangeWhileOff = roadKmRange.textContent;
        roadKmEnableCheckbox.checked = true;
        applyRoadKmActivation();
        return {rangeWhileOff, rangeOn: roadKmRange.textContent};
    }""")
    assert result["rangeWhileOff"] == ""
    assert "Участок" in result["rangeOn"]
    assert "разрывы" in result["rangeOn"]


def test_address_hint_clears_when_input_shrinks(page, app_server):
    """При сокращении адреса до <3 символов старый статус не остаётся."""
    login(page, app_server["base_url"])
    result = page.evaluate("""() => {
        searchDraft.mode = 'address';
        modeInput1.value = 'Ростов';
        modeInput1.dispatchEvent(new Event('input', {bubbles: true}));
        const longStatus = modeStatus.textContent;
        modeInput1.value = '  ';
        modeInput1.dispatchEvent(new Event('input', {bubbles: true}));
        return {longStatus, shortStatus: modeStatus.textContent};
    }""")
    assert "Выберите адрес" in result["longStatus"]
    assert result["shortStatus"] == ""


def test_crs_toggle_converts_uncommitted_draft(page, app_server):
    """Без зафиксированной точки переключение WGS/МСК конвертирует ввод."""
    login(page, app_server["base_url"])
    result = page.evaluate("""async () => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
        apiFetch = async (url) => {
            if (url === '/api/msk61/from-geographic') {
                return ok({x: 422630.864, y: 2204440.047, zone: 2,
                           profile: 'gost_51794_2008', warning: ''});
            }
            if (url === '/api/msk61/to-geographic') {
                return ok({lat: 47.23, lon: 39.72, zone: 2,
                           profile: 'gost_51794_2008', can_place_on_map: true, warning: ''});
            }
            return ok({});
        };
        activePoint = null;
        availableMskZone = null;
        mskZone = 2;
        searchDraft.mode = 'coords';
        crsState = 'gps';
        updateCrsLabel();
        modeInput1.value = '47.23, 39.72';
        modeInput1.dispatchEvent(new Event('input', {bubbles: true}));
        cycleCrs();
        await sleep(100);
        const msk = {
            crs: crsState,
            value: modeInput1.value,
            reference: modeInput2.value,
            lat: searchDraft.coordLat,
            lon: searchDraft.coordLon,
        };
        cycleCrs();
        await sleep(100);
        return {
            msk,
            wgs: {crs: crsState, value: modeInput1.value, reference: modeInput2.value},
        };
    }""")
    assert result["msk"]["crs"] == "msk2"
    assert result["msk"]["value"].startswith("422630.864")
    assert result["msk"]["reference"].startswith("47.230000")
    assert result["msk"]["lat"] == pytest.approx(47.23, abs=1e-6)
    assert result["wgs"]["crs"] == "gps", result
    assert result["wgs"]["value"].startswith("47.230000, 39.720000"), result


def test_vector_map_source_requires_archive(page, app_server):
    """Без vector.mbtiles режим MapLibre явно сообщает об отсутствии архива."""
    login(page, app_server["base_url"])
    page.wait_for_function("uiState.ready", timeout=15000)
    assert page.evaluate("vectorTilesAvailable") is False
    page.evaluate("""() => {
        const select = document.getElementById('mapSourceSelect');
        select.value = 'vector';
        select.dispatchEvent(new Event('change'));
    }""")
    page.wait_for_selector("#map-source-notice.visible", timeout=5000)
    assert "Векторный архив" in page.locator("#map-source-notice").inner_text()
    assert page.evaluate("Boolean(vectorBaseLayer)") is False
    page.evaluate("""() => {
        const select = document.getElementById('mapSourceSelect');
        select.value = 'external';
        select.dispatchEvent(new Event('change'));
    }""")
    page.wait_for_function("Boolean(baseTileLayer)", timeout=5000)


def test_point_labels_toggle_shows_all_map_tooltips(page, app_server):
    """Кнопка 💬 показывает постоянные подписи активной, пакетных точек и призрака."""
    login(page, app_server["base_url"])
    page.evaluate("""async () => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        apiFetch = async () => ok({point: {lat: 47.2, lon: 39.8}, road: null, side: null});
        commitPoint(47.2, 39.8, 'click');
        await new Promise(resolve => setTimeout(resolve, 80));
        if (!ghostPointEnabled) toggleGhostPoint();
        batchPoints = [{id: 'label-1', name: 'Тестовая', lat: 47.3, lon: 39.9}];
        renderBatchMarkers(false);
    }""")
    page.click("#pointLabelsToggle")
    page.wait_for_function("document.querySelectorAll('.leaflet-tooltip.point-label').length >= 3",
                           timeout=5000)
    assert page.locator("#pointLabelsToggle").get_attribute("aria-pressed") == "true"
    labels = page.locator(".leaflet-tooltip.point-label").all_inner_texts()
    assert any("Призрак" in text for text in labels)
    assert any("Тестовая" in text for text in labels)
    page.click("#pointLabelsToggle")
    assert page.locator("#pointLabelsToggle").get_attribute("aria-pressed") == "false"
    page.wait_for_function("document.querySelectorAll('.leaflet-tooltip.point-label').length === 0",
                           timeout=5000)


def test_road_tooltip_is_clean_and_hover_radius_is_configurable(page, app_server):
    """Тултип дороги — только километраж на белом фоне; радиус меняется в настройках."""
    login(page, app_server["base_url"])
    page.evaluate("""() => {
        map.setView([47.20, 39.80], 14, {animate: false});
        drawRoadVector({
            start_km: 0,
            finish_km: 20,
            geometry: {type: 'LineString', coordinates: [[39.70, 47.20], [39.90, 47.20]]},
        }, {});
        const input = document.getElementById('roadHoverRadiusInput');
        input.value = '20';
        input.dispatchEvent(new Event('change', {bubbles: true}));
    }""")
    assert page.evaluate("roadHoverRadius") == 20
    assert page.evaluate("Number(localStorage.getItem('albion_road_hover_radius'))") == 20
    # 0.001° по широте на z14 — около 12 px, попадает в радиус 20.
    page.evaluate("map.fire('mousemove', {latlng: L.latLng(47.201, 39.80)})")
    page.wait_for_selector(".road-tooltip", timeout=5000)
    text = page.locator(".road-tooltip").inner_text()
    assert "км" in text and "📍" not in text
    style = page.evaluate("""() => {
        const el = document.querySelector('.road-tooltip');
        return {
            background: getComputedStyle(el).backgroundColor,
            beforeDisplay: getComputedStyle(el, '::before').display,
        };
    }""")
    assert style["background"] == "rgb(255, 255, 255)"
    assert style["beforeDisplay"] == "none"
    # При радиусе 4 та же точка уже вне зоны — тултип скрывается.
    page.evaluate("""() => {
        const input = document.getElementById('roadHoverRadiusInput');
        input.value = '4';
        input.dispatchEvent(new Event('change', {bubbles: true}));
        map.fire('mousemove', {latlng: L.latLng(47.201, 39.80)});
    }""")
    page.wait_for_function("!document.querySelector('.road-tooltip')", timeout=5000)
    page.evaluate("""() => {
        const input = document.getElementById('roadHoverRadiusInput');
        input.value = '12';
        input.dispatchEvent(new Event('change', {bubbles: true}));
        clearRoadVectorLayer();
    }""")


def test_local_road_without_chainage_can_place_fragment_point(page, app_server):
    """Локальная дорога без официального км ставит точку на выбранный фрагмент."""
    login(page, app_server["base_url"])
    result = page.evaluate("""async () => {
        const ok = data => new Response(JSON.stringify(data), {
            status: 200, headers: {'Content-Type': 'application/json'}
        });
        const original = apiFetch;
        apiFetch = async (url, options) => {
            if (url.startsWith('/api/roads/search')) {
                return ok({roads: [{
                    id: 'osm-way:1', ref: '60К-16', name: 'М4 Дон - Вёшенская',
                    selected_km: null, selected_lat: 48.99, selected_lon: 40.55,
                    source: 'OpenStreetMap', km_provenance: 'no authoritative chainage; geometry length only',
                }]});
            }
            if (url === '/api/locate') {
                return ok({point: {lat: 48.99, lon: 40.55}, road: null, side: null});
            }
            return original(url, options);
        };
        activateFrameTab('point');
        useLocalRoadData = true;
        setSearchMode('road');
        searchArmCheckbox.checked = true;
        applySearchArmState();
        modeInput1.value = 'Дон';
        modeInput1.dispatchEvent(new Event('input', {bubbles: true}));
        await searchLocalRoad();
        document.querySelector('#modeSuggestions .geo-suggestion').click();
        const prepared = {
            status: modeStatus.textContent,
            roadKm: searchDraft.roadKm,
            lat: searchDraft.roadLat,
        };
        await runSearchAction();
        await new Promise(resolve => setTimeout(resolve, 120));
        return {
            prepared,
            active: activePoint ? {
                lat: activePoint.lat, lon: activePoint.lon, source: activePoint.source,
                roadLabel: activePoint.roadLabel,
            } : null,
            armed: searchArmCheckbox.checked,
        };
    }""")
    assert result["prepared"]["roadKm"] is None
    assert result["prepared"]["lat"] == pytest.approx(48.99, abs=1e-6)
    assert "километраж" in result["prepared"]["status"] or "километр" in result["prepared"]["status"]
    assert result["active"]["source"] == "road"
    assert result["active"]["roadLabel"] == "М4 Дон - Вёшенская"
    assert result["active"]["lat"] == pytest.approx(48.99, abs=1e-6)
    assert result["armed"] is False


def test_km_slider_stays_visible_when_km_state_is_cleared(page, app_server):
    """Сброс километража не должен скрывать панель и ползунок справа."""
    login(page, app_server["base_url"])
    page.evaluate("""() => {
        roadKmTitle.textContent = 'М-4';
        roadKmState = buildRoadKmState({
            first_km: 0, last_km: 20,
            sections: [{start_km: 0, finish_km: 20,
                        coordinates: [[39.70, 47.20], [39.90, 47.20]]}],
        });
        roadKmEnableCheckbox.checked = true;
        applyRoadKmActivation();
        clearLoadedRoadKmState('Километраж сброшен.');
    }""")
    assert page.locator("#roadKmPanel").is_visible()
    assert page.locator("#roadKmSlider").is_visible()
    assert page.locator("#roadKmSlider").is_disabled()


def test_account_modal_opens_from_status_bar(page, app_server):
    """Кнопка 👤 открывает аккаунт; Esc и фон закрывают окно."""
    login(page, app_server["base_url"])
    page.click("#accountOpenButton")
    assert page.locator("#account-modal").evaluate("el => el.classList.contains('open')")
    assert page.locator("#accountLogin").inner_text() == "админ"
    assert "Администратор" in page.locator("#accountRole").inner_text()
    assert page.locator("#accountForcedHint").is_hidden()
    page.keyboard.press("Escape")
    assert not page.locator("#account-modal").evaluate("el => el.classList.contains('open')")


def test_forced_password_change_on_bootstrap(browser, tmp_path):
    """Пустая БД с ALBION_REQUIRE_PASSWORD_CHANGE=1 требует смены пароля."""
    import os
    import socket
    import subprocess
    import sys
    import time
    import urllib.request
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ)
    env.update({
        "ALBION_USERS_DB": str(tmp_path / "users.db"),
        "ALBION_SETTINGS_FILE": str(tmp_path / "user_settings.json"),
        "ALBION_GEO_DATA_DIR": str(tmp_path / "geo"),
        "ALBION_BASEMAP_MBTILES": str(tmp_path / "basemap.mbtiles"),
        "ALBION_VECTOR_MBTILES": str(tmp_path / "vector.mbtiles"),
        "ALBION_REQUIRE_PASSWORD_CHANGE": "1",
        "YANDEX_API_KEY": "",
        "YANDEX_SUGGEST_API_KEY": "",
        "YANDEX_JS_API_KEY": "",
    })
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--app-dir", str(root / "src"),
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(root), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        deadline = time.time() + 20
        healthy = False
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{base_url}/api/health", timeout=1) as response:
                    if response.status == 200:
                        healthy = True
                        break
            except OSError:
                time.sleep(0.15)
        assert healthy, "forced-password server did not start"
        page.goto(base_url)
        page.fill("#authLogin", "админ")
        page.fill("#authPassword", "альбион")
        page.click("#authSubmit")
        page.wait_for_selector("body.authenticated")
        page.wait_for_selector("#account-modal.open", timeout=5000)
        assert page.locator("#accountForcedHint").is_visible()
        assert page.locator("#account-dialog-close").is_hidden()
        page.keyboard.press("Escape")
        assert page.locator("#account-modal").evaluate("el => el.classList.contains('open')")
        page.fill("#accountCurrentPassword", "альбион")
        page.fill("#accountNewPassword", "НовыйПароль123")
        page.fill("#accountConfirmPassword", "НовыйПароль123")
        with page.expect_navigation():
            page.click("#accountSaveBtn")
        page.wait_for_selector("body.authenticated")
        page.wait_for_selector("#account-modal.open", state="detached", timeout=5000)
        assert page.evaluate(
            "async () => (await (await fetch('/api/auth/me')).json()).user.must_change_password"
        ) is False
        # После смены пароля остальные API снова доступны.
        page.click("#settingsOpenButton")
        assert page.locator("#settings-modal").evaluate("el => el.classList.contains('open')")
    finally:
        context.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
