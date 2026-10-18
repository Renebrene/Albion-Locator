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

    return {
        EARTH_RADIUS_KM,
        suggestMskZone,
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
