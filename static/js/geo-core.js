/*
 * АЛЬБИОН: гео-ядро клиента.
 *
 * Чистые функции без DOM и без сети: парсинг координат, раскладка клавиатуры,
 * проекция точки на линию и перевод ближайшего места в официальный километр.
 * Модуль работает и в браузере (window.AlbionGeo), и в Bun (module.exports),
 * поэтому его можно покрыть тестами без запуска сервера.
 */
(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root) root.AlbionGeo = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';

    const EARTH_RADIUS_KM = 6371.0088;
    const LAT_LIMIT = 90;
    const LON_LIMIT = 180;

    // ===== РАСКЛАДКА КЛАВИАТУРЫ =====
    // Латиница QWERTY -> кириллица ЙЦУКЕН. Нужна, когда пользователь забыл
    // переключить раскладку и набрал «rjhnbyfns» вместо «координаты».
    const LATIN_TO_CYRILLIC = {
        q: 'й', w: 'ц', e: 'у', r: 'к', t: 'е', y: 'н', u: 'г', i: 'ш', o: 'щ', p: 'з',
        '[': 'х', ']': 'ъ',
        a: 'ф', s: 'ы', d: 'в', f: 'а', g: 'п', h: 'р', j: 'о', k: 'л', l: 'д',
        ';': 'ж', "'": 'э',
        z: 'я', x: 'ч', c: 'с', v: 'м', b: 'и', n: 'т', m: 'ь',
        ',': 'б', '.': 'ю',
    };

    function looksLikeWrongLayout(value) {
        const text = String(value || '');
        if (!/[A-Za-z]/.test(text)) return false;
        // Если латиница есть, а кириллицы нет — раскладка почти наверняка не та.
        // Смешанный ввод (латинский код дороги + кириллица) не трогаем.
        if (/[А-Яа-яЁё]/.test(text)) return false;
        // Цифры и дефис встречаются в кодах дорог (M-4, R-280) и километраже,
        // где латиница осмысленна, поэтому такие строки не переводим.
        if (/\d/.test(text)) return false;
        return true;
    }

    /**
     * Переводит случайно набранную латиницу в кириллицу.
     * Регистр сохраняется, цифры и символы без пары остаются как есть.
     */
    function toCyrillicLayout(value) {
        return String(value || '').replace(/[A-Za-z\[\];',.']/g, (char) => {
            const mapped = LATIN_TO_CYRILLIC[char.toLowerCase()];
            if (!mapped) return char;
            return char === char.toUpperCase() ? mapped.toUpperCase() : mapped;
        });
    }

    /**
     * Нормализует поисковый запрос: если раскладка явно не та — переводит в
     * кириллицу. Исходный текст не портит: границы уже починенного текста
     * вызывающий код показывает пользователю отдельно.
     */
    function normalizeSearchQuery(value) {
        const text = String(value == null ? '' : value);
        const normalized = text;
        if (!looksLikeWrongLayout(normalized)) {
            return { text: normalized, changed: false, original: text };
        }
        const converted = toCyrillicLayout(normalized);
        if (converted === normalized) {
            return { text: normalized, changed: false, original: text };
        }
        return { text: converted, changed: true, original: text };
    }

    // ===== КООРДИНАТЫ =====

    function isFiniteNumber(value) {
        return typeof value === 'number' && Number.isFinite(value);
    }

    function inGpsRange(lat, lon) {
        return isFiniteNumber(lat) && isFiniteNumber(lon)
            && Math.abs(lat) <= LAT_LIMIT && Math.abs(lon) <= LON_LIMIT;
    }

    const HEMISPHERE_REF = { N: 1, E: 1, S: -1, W: -1 };

    function parseDecimalNumber(raw) {
        if (raw == null) return null;
        const text = String(raw).trim().replace(',', '.');
        if (!text || !/^[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?$/.test(text)) return null;
        const value = Number(text);
        return Number.isFinite(value) ? value : null;
    }

    /**
     * Разбирает компоненту DMS вида `47°14'42.2"N`, `47 14 42.2`, `14°42.2'`.
     * Возвращает десятичные градусы со знаком или null.
     */
    function parseDmsComponent(raw) {
        if (raw == null) return null;
        const text = String(raw).trim().replace(',', '.');
        if (!text) return null;
        const hemisphere = /([NSEWnsew])\s*$/.exec(text);
        const ref = hemisphere ? hemisphere[1].toUpperCase() : null;
        const body = hemisphere ? text.slice(0, hemisphere.index) : text;
        const numbers = body.match(/\d+(?:\.\d+)?/g);
        if (!numbers || numbers.length === 0 || numbers.length > 3) return null;
        const degrees = Number(numbers[0]);
        const minutes = numbers.length > 1 ? Number(numbers[1]) : 0;
        const seconds = numbers.length > 2 ? Number(numbers[2]) : 0;
        if (![degrees, minutes, seconds].every(Number.isFinite)) return null;
        if (minutes < 0 || minutes >= 60 || seconds < 0 || seconds >= 60) return null;
        const sign = ref ? HEMISPHERE_REF[ref] : 1;
        const value = degrees + minutes / 60 + seconds / 3600;
        if (degrees < 0) return -Math.abs(value);
        return sign * Math.abs(value);
    }

    const DMS_GROUP_RE = /[-+]?\d+(?:[.,]\d+)?\s*(?:°|d|deg)\s*(?:\d+(?:[.,]\d+)?\s*(?:['′]|m|min)\s*)?(?:\d+(?:[.,]\d+)?\s*(?:"|″|s|sec)\s*)?[NSEWnsew]?/gi;

    /**
     * Разбивает строку на две компоненты координат.
     * Возвращает [component1, component2] или null.
     */
    function splitComponents(text) {
        const value = String(text).trim();
        if (!value) return null;

        // DMS со знаками градуса/минуты/секунды.
        const dmsGroups = value.match(DMS_GROUP_RE);
        if (dmsGroups && dmsGroups.length >= 2) {
            return [dmsGroups[0], dmsGroups[1]];
        }

        // «47 14 42 N 39 40 03 E» — две DMS-группы по четыре токена.
        const tokens = value.match(/\d+(?:[.,]\d+)?|[NSEWnsew]/g);
        if (tokens && tokens.length === 8 && /[NSEWnsew]/.test(tokens[3])
            && /[NSEWnsew]/.test(tokens[7])) {
            return [tokens.slice(0, 4).join(' '), tokens.slice(4, 8).join(' ')];
        }

        // Десятичные: пробел/таб/перевод строки как разделитель.
        const bySpace = value.split(/[\s\n]+/).map(item => item.trim()).filter(Boolean);
        if (bySpace.length === 2
            && parseDecimalNumber(bySpace[0]) !== null
            && parseDecimalNumber(bySpace[1]) !== null) {
            return bySpace;
        }

        // Десятичные: запятая/точка с запятой/слэш как разделитель.
        const byPunct = value.split(/[,;\/]+/).map(item => item.trim()).filter(Boolean);
        if (byPunct.length === 2) return byPunct;

        return null;
    }

    /**
     * Разбирает координаты в WGS84. Поддерживает десятичные градусы и DMS с
     * N/S/E/W, любой порядок lat/lon, десятичную запятую.
     * Возвращает { lat, lon, format } или null.
     */
    function parseWgsCoordinates(input) {
        if (input == null) return null;
        const text = String(input).trim();
        if (!text) return null;

        const components = splitComponents(text);
        if (!components) return null;

        const first = parseDmsComponent(components[0]);
        const second = parseDmsComponent(components[1]);
        if (!isFiniteNumber(first) || !isFiniteNumber(second)) return null;

        const hasHemisphere = /[NSEWnsew]/.test(text);
        let lat = first;
        let lon = second;
        // Без явных направлений: если первое значение выходит за широту, а
        // второе — нет, считаем, что порядок «долгота, широта».
        if (!hasHemisphere && Math.abs(first) > LAT_LIMIT) {
            if (Math.abs(second) > LAT_LIMIT) return null;
            lat = second;
            lon = first;
        }
        if (!inGpsRange(lat, lon)) return null;

        const format = /°|['′]|["″]|[NSEWnsew]/.test(text) ? 'dms' : 'decimal';
        return { lat, lon, format };
    }

    /** Форматирует градусы в строку вида 47°14'42.2"N. */
    /** По долготе определяет вероятную зону МСК-61 для Ростовской области. */
    function suggestMskZone(lon) {
        if (!isFiniteNumber(lon)) return 2;
        // Зоны по центральным меридианам: зона 1 = 38°, зона 2 = 41°, зона 3 = 44°.
        if (lon < 39.2) return 1;
        if (lon > 42.5) return 3;
        return 2;
    }

    function formatDms(value, axis) {
        if (!isFiniteNumber(value)) return '';
        const negative = value < 0;
        const absolute = Math.abs(value);
        let degrees = Math.floor(absolute);
        const minutesFloat = (absolute - degrees) * 60;
        let minutes = Math.floor(minutesFloat);
        let seconds = (minutesFloat - minutes) * 60;
        seconds = Math.round(seconds * 10) / 10;
        if (seconds >= 60) { seconds -= 60; minutes += 1; }
        if (minutes >= 60) { minutes -= 60; degrees += 1; }
        const ref = axis === 'lat' ? (negative ? 'S' : 'N') : (negative ? 'W' : 'E');
        return `${degrees}°${String(minutes).padStart(2, '0')}'${String(seconds.toFixed(1)).padStart(4, '0')}"${ref}`;
    }

    // ===== ГЕОМЕТРИЯ =====

    function haversineKm(lon1, lat1, lon2, lat2) {
        const radians = Math.PI / 180;
        const dLat = (lat2 - lat1) * radians;
        const dLon = (lon2 - lon1) * radians;
        const a = Math.sin(dLat / 2) ** 2
            + Math.cos(lat1 * radians) * Math.cos(lat2 * radians) * Math.sin(dLon / 2) ** 2;
        return EARTH_RADIUS_KM * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
    }

    /**
     * Проекция точки на отрезок в локальной метрической системе. Возвращает
     * { lon, lat, t, distanceKm }, где t — доля вдоль отрезка.
     */
    function projectOnSegment(lon, lat, startLon, startLat, endLon, endLat) {
        const radians = Math.PI / 180;
        const metersPerDegLat = 111132.92 - 559.82 * Math.cos(2 * lat * radians)
            + 1.175 * Math.cos(4 * lat * radians);
        const metersPerDegLon = 111412.84 * Math.cos(lat * radians)
            - 93.5 * Math.cos(3 * lat * radians)
            + 0.118 * Math.cos(5 * lat * radians);
        const toX = (lonValue) => lonValue * metersPerDegLon;
        const toY = (latValue) => latValue * metersPerDegLat;
        const ax = toX(startLon), ay = toY(startLat);
        const bx = toX(endLon), by = toY(endLat);
        const px = toX(lon), py = toY(lat);
        const dx = bx - ax, dy = by - ay;
        const lengthSquared = dx * dx + dy * dy;
        let t = lengthSquared > 0 ? ((px - ax) * dx + (py - ay) * dy) / lengthSquared : 0;
        t = Math.max(0, Math.min(1, t));
        const projectedLon = startLon + (endLon - startLon) * t;
        const projectedLat = startLat + (endLat - startLat) * t;
        const distanceMeters = Math.hypot(px - (ax + dx * t), py - (ay + dy * t));
        return {
            lon: projectedLon,
            lat: projectedLat,
            t,
            distanceKm: distanceMeters / 1000,
        };
    }

    /**
     * Ближайшая точка к (lon, lat) среди набора линий.
     * lines: [[[lon, lat], ...], ...]. Возвращает { lon, lat, lineIndex,
     * segmentIndex, t, distanceKm } или null.
     */
    function nearestPointOnLines(lon, lat, lines) {
        let best = null;
        (lines || []).forEach((line, lineIndex) => {
            if (!Array.isArray(line) || line.length < 2) return;
            for (let index = 1; index < line.length; index += 1) {
                const start = line[index - 1];
                const end = line[index];
                if (!Array.isArray(start) || !Array.isArray(end)) continue;
                const projection = projectOnSegment(
                    lon, lat, start[0], start[1], end[0], end[1],
                );
                if (!best || projection.distanceKm < best.distanceKm) {
                    best = { ...projection, lineIndex, segmentIndex: index - 1 };
                }
            }
        });
        return best;
    }

    /**
     * Ближайший официальный километр по участкам трассы.
     * sections: [{ coordinates: [[lon, lat], ...], start_km, finish_km }].
     * Сравнение идёт с отрезком, а не только с вершинами, поэтому редкие
     * вершины геометрии больше не дают грубую ошибку.
     * Возвращает { officialKm, distanceKm, sectionIndex, lon, lat } или null.
     */
    function nearestOfficialKmOnSections(lon, lat, sections) {
        let best = null;
        (sections || []).forEach((section, sectionIndex) => {
            const coordinates = section && section.coordinates;
            if (!Array.isArray(coordinates) || coordinates.length < 2) return;
            const startKm = Number(section.start_km);
            const finishKm = Number(section.finish_km);
            if (!Number.isFinite(startKm) || !Number.isFinite(finishKm) || startKm === finishKm) return;
            // Доля вдоль линии -> интерполяция официального километра.
            const lengths = [0];
            let total = 0;
            for (let index = 1; index < coordinates.length; index += 1) {
                total += haversineKm(
                    coordinates[index - 1][0], coordinates[index - 1][1],
                    coordinates[index][0], coordinates[index][1],
                );
                lengths.push(total);
            }
            if (!(total > 0)) return;
            const projection = nearestPointOnLines(lon, lat, [coordinates]);
            if (!projection) return;
            const metresAlong = projection.t * lengths[projection.segmentIndex + 1]
                + (1 - projection.t) * lengths[projection.segmentIndex];
            const fraction = metresAlong / total;
            const officialKm = startKm + (finishKm - startKm) * fraction;
            if (!best || projection.distanceKm < best.distanceKm) {
                best = {
                    officialKm,
                    distanceKm: projection.distanceKm,
                    sectionIndex,
                    lon: projection.lon,
                    lat: projection.lat,
                };
            }
        });
        return best;
    }

    // ===== КОМПАКТНАЯ ССЫЛКА НА СОСТОЯНИЕ =====
    // Формат: v1 + сегменты «буква + значение фиксированной ширины».
    // Пример: v1l29pevbo3mt74pzgmctpk00jmek
    // lat/lon кодируются как смещённые целые микроградусы в base36,
    // поэтому знаки и десятичные точки не занимают места.
    const URL_STATE_VERSION = 'v1';
    const BASE36_DIGITS = '0123456789abcdefghijklmnopqrstuvwxyz';
    const URL_STATE_MODES = { address: 'a', coords: 'c', road: 'r' };
    const URL_STATE_TABS = { point: 'p', batch: 'b', saved: 's' };
    const URL_STATE_MODE_CODES = { a: 'address', c: 'coords', r: 'road' };
    const URL_STATE_TAB_CODES = { p: 'point', b: 'batch', s: 'saved' };
    const URL_STATE_SEGMENT_WIDTH = { l: 6, o: 6, z: 1, m: 1, t: 1, k: 6 };
    const LAT_OFFSET_MICRO = 90_000_000;
    const LON_OFFSET_MICRO = 180_000_000;
    const MAX_KM_METRES = 99_999_999;

    function base36Encode(value, width) {
        let number = Math.round(Number(value));
        if (!Number.isFinite(number) || number < 0) number = 0;
        let text = '';
        do {
            text = BASE36_DIGITS[number % 36] + text;
            number = Math.floor(number / 36);
        } while (number > 0);
        return text.padStart(width, '0');
    }

    function base36Decode(text) {
        if (typeof text !== 'string' || !/^[0-9a-z]+$/.test(text)) return null;
        const value = parseInt(text, 36);
        return Number.isFinite(value) ? value : null;
    }

    /**
     * Кодирует состояние приложения в компактную строку без разделителей.
     * state: { lat, lon, zoom, mode, tab, km } — все поля необязательны.
     */
    function encodeUrlState(state) {
        const data = state && typeof state === 'object' ? state : {};
        const parts = [URL_STATE_VERSION];
        const lat = Number(data.lat);
        const lon = Number(data.lon);
        if (isFiniteNumber(lat) && isFiniteNumber(lon) && inGpsRange(lat, lon)) {
            parts.push('l' + base36Encode(Math.round(lat * 1e6) + LAT_OFFSET_MICRO, 6));
            parts.push('o' + base36Encode(Math.round(lon * 1e6) + LON_OFFSET_MICRO, 6));
        }
        const zoom = Math.round(Number(data.zoom));
        if (Number.isFinite(zoom)) {
            parts.push('z' + base36Encode(Math.max(1, Math.min(19, zoom)), 1));
        }
        if (URL_STATE_MODES[data.mode]) parts.push('m' + URL_STATE_MODES[data.mode]);
        if (URL_STATE_TABS[data.tab]) parts.push('t' + URL_STATE_TABS[data.tab]);
        const km = Number(data.km);
        if (Number.isFinite(km) && km >= 0) {
            parts.push('k' + base36Encode(Math.min(Math.round(km * 1000), MAX_KM_METRES), 6));
        }
        return parts.join('');
    }

    /**
     * Разбирает строку формата v1... Возвращает
     * { point?, zoom?, mode?, tab?, km? } или null.
     */
    function decodeUrlState(value) {
        const text = String(value == null ? '' : value).trim().replace(/^#/, '');
        if (!text.startsWith(URL_STATE_VERSION)) return null;
        let index = URL_STATE_VERSION.length;
        let lat = null;
        let lon = null;
        let zoom = null;
        let mode = null;
        let tab = null;
        let km = null;

        while (index < text.length) {
            const code = text[index];
            const width = URL_STATE_SEGMENT_WIDTH[code];
            if (!width) return null;
            const segment = text.slice(index + 1, index + 1 + width);
            if (segment.length !== width) return null;
            index += 1 + width;
            const number = base36Decode(segment);
            if (code === 'l') {
                if (number === null || number < 0 || number > LAT_OFFSET_MICRO * 2) return null;
                lat = number / 1e6 - 90;
            } else if (code === 'o') {
                if (number === null || number < 0 || number > LON_OFFSET_MICRO * 2) return null;
                lon = number / 1e6 - 180;
            } else if (code === 'z') {
                if (number === null || number < 1 || number > 19) return null;
                zoom = number;
            } else if (code === 'm') {
                mode = URL_STATE_MODE_CODES[segment] || null;
                if (!mode) return null;
            } else if (code === 't') {
                tab = URL_STATE_TAB_CODES[segment] || null;
                if (!tab) return null;
            } else if (code === 'k') {
                if (number === null || number > MAX_KM_METRES) return null;
                km = number / 1000;
            }
        }

        const state = {};
        if (lat !== null && lon !== null) {
            if (!inGpsRange(lat, lon)) return null;
            state.point = { lat, lon };
        }
        if (zoom !== null) state.zoom = zoom;
        if (mode) state.mode = mode;
        if (tab) state.tab = tab;
        if (km !== null) state.km = km;
        return Object.keys(state).length ? state : null;
    }

    return {
        EARTH_RADIUS_KM,
        suggestMskZone,
        encodeUrlState,
        decodeUrlState,
        LATIN_TO_CYRILLIC,
        looksLikeWrongLayout,
        toCyrillicLayout,
        normalizeSearchQuery,
        parseDecimalNumber,
        parseDmsComponent,
        parseWgsCoordinates,
        formatDms,
        inGpsRange,
        haversineKm,
        projectOnSegment,
        nearestPointOnLines,
        nearestOfficialKmOnSections,
    };
});
