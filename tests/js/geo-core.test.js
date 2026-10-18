/*
 * Тесты гео-ядра клиента. Запуск: bun test tests/js
 */
import { describe, expect, test } from 'bun:test';
import geo from '../../static/js/geo-core.js';

describe('parseWgsCoordinates', () => {
    test('десятичные градусы через запятую', () => {
        const result = geo.parseWgsCoordinates('47.23, 39.72');
        expect(result.lat).toBeCloseTo(47.23, 6);
        expect(result.lon).toBeCloseTo(39.72, 6);
        expect(result.format).toBe('decimal');
    });

    test('десятичные градусы через пробел и точку с запятой', () => {
        expect(geo.parseWgsCoordinates('47.23 39.72').lon).toBeCloseTo(39.72, 6);
        expect(geo.parseWgsCoordinates('47.23; 39.72').lon).toBeCloseTo(39.72, 6);
    });

    test('десятичная запятая как разделитель дробной части', () => {
        const result = geo.parseWgsCoordinates('47,23 39,72');
        expect(result.lat).toBeCloseTo(47.23, 6);
        expect(result.lon).toBeCloseTo(39.72, 6);
    });

    test('DMS с направлениями', () => {
        const result = geo.parseWgsCoordinates('47°14\'42.2"N 39°40\'03.2"E');
        expect(result.lat).toBeCloseTo(47.24506, 4);
        expect(result.lon).toBeCloseTo(39.66756, 4);
        expect(result.format).toBe('dms');
    });

    test('DMS с южным и западным направлением даёт минус', () => {
        const result = geo.parseWgsCoordinates('33°51\'30"S 151°12\'30"E');
        expect(result.lat).toBeLessThan(0);
    });

    test('порядок «долгота, широта» распознаётся по диапазону', () => {
        // Значение 139.72 не может быть широтой -> порядок меняется.
        const result = geo.parseWgsCoordinates('139.72, 47.23');
        expect(result.lat).toBeCloseTo(47.23, 6);
        expect(result.lon).toBeCloseTo(139.72, 6);
    });

    test('явно неверные значения отклоняются', () => {
        expect(geo.parseWgsCoordinates('')).toBeNull();
        expect(geo.parseWgsCoordinates('47.23')).toBeNull();
        expect(geo.parseWgsCoordinates('abc, def')).toBeNull();
        expect(geo.parseWgsCoordinates('95, 91')).toBeNull();
        expect(geo.parseWgsCoordinates('47, 181')).toBeNull();
    });

    test('минуты/секунды вне диапазона отклоняются', () => {
        expect(geo.parseWgsCoordinates('47°60\'00"N 39°40\'00"E')).toBeNull();
        expect(geo.parseDmsComponent('47°14\'60"N')).toBeNull();
    });

    test('null и undefined безопасны', () => {
        expect(geo.parseWgsCoordinates(null)).toBeNull();
        expect(geo.parseWgsCoordinates(undefined)).toBeNull();
    });
});

describe('formatDms', () => {
    test('форматирует широту на север', () => {
        expect(geo.formatDms(47.24506, 'lat')).toBe('47°14\'42.2"N');
    });

    test('форматирует юг и запад', () => {
        expect(geo.formatDms(-33.85833, 'lat')).toContain('"S');
        expect(geo.formatDms(-70.5, 'lon')).toContain('"W');
    });

    test('округление секунд не ломает минуты', () => {
        const text = geo.formatDms(47.99999999, 'lat');
        expect(text).toContain('48°00\'00.0"N');
    });
});

describe('normalizeSearchQuery', () => {
    test('латиница на неправильной раскладке переводится в кириллицу', () => {
        const result = geo.normalizeSearchQuery('Rjjhlbyfns');
        expect(result.changed).toBe(true);
        expect(result.text).toBe('Координаты');
        expect(result.original).toBe('Rjjhlbyfns');
    });

    test('корректный русский текст не меняется', () => {
        const result = geo.normalizeSearchQuery('Ростов-на-Дону');
        expect(result.changed).toBe(false);
        expect(result.text).toBe('Ростов-на-Дону');
    });

    test('код дороги с латиницей не портится при смешанном вводе', () => {
        const result = geo.normalizeSearchQuery('M-4 Дон');
        expect(result.changed).toBe(false);
        expect(result.text).toBe('M-4 Дон');
    });

    test('пустое значение безопасно', () => {
        expect(geo.normalizeSearchQuery('').changed).toBe(false);
        expect(geo.normalizeSearchQuery(null).text).toBe('');
    });
});

describe('projectOnSegment', () => {
    test('точка в середине отрезка проецируется с t≈0.5', () => {
        const projection = geo.projectOnSegment(39.72, 47.201, 39.70, 47.20, 39.74, 47.20);
        expect(projection.t).toBeCloseTo(0.5, 1);
        expect(projection.distanceKm).toBeCloseTo(0.111, 1);
    });

    test('проекция за концами обрезается до крайних точек', () => {
        const projection = geo.projectOnSegment(39.90, 47.20, 39.70, 47.20, 39.74, 47.20);
        expect(projection.t).toBeCloseTo(1, 6);
        expect(projection.lon).toBeCloseTo(39.74, 6);
    });

    test('вырожденный отрезок не делит на ноль', () => {
        const projection = geo.projectOnSegment(39.70, 47.20, 39.70, 47.20, 39.70, 47.20);
        expect(projection.t).toBe(0);
        expect(Number.isFinite(projection.distanceKm)).toBe(true);
    });
});

describe('nearestOfficialKmOnSections', () => {
    test('ближайший километр считается по отрезку, а не по вершинам', () => {
        const sections = [{
            coordinates: [[39.70, 47.20], [39.74, 47.20]],
            start_km: 0,
            finish_km: 10,
        }];
        const nearest = geo.nearestOfficialKmOnSections(39.72, 47.20, sections);
        expect(nearest.officialKm).toBeCloseTo(5, 0);
        expect(nearest.distanceKm).toBeLessThan(0.01);
    });

    test('редкая вершина не даёт грубую ошибку', () => {
        // Один длинный отрезок: точка точно посередине должна дать ~середину км.
        const sections = [{
            coordinates: [[39.0, 47.0], [40.0, 47.0]],
            start_km: 100,
            finish_km: 200,
        }];
        const nearest = geo.nearestOfficialKmOnSections(39.5, 47.0, sections);
        expect(nearest.officialKm).toBeCloseTo(150, 0);
    });

    test('выбирается ближайший из нескольких участков', () => {
        const sections = [
            { coordinates: [[39.70, 47.20], [39.74, 47.20]], start_km: 0, finish_km: 10 },
            { coordinates: [[39.80, 47.30], [39.84, 47.30]], start_km: 10, finish_km: 20 },
        ];
        // Точка ровно посередине второго участка -> середина диапазона 10..20.
        const nearest = geo.nearestOfficialKmOnSections(39.82, 47.30, sections);
        expect(nearest.sectionIndex).toBe(1);
        expect(nearest.officialKm).toBeCloseTo(15, 0);
    });

    test('пустые и битые участки игнорируются', () => {
        expect(geo.nearestOfficialKmOnSections(39.72, 47.20, [])).toBeNull();
        expect(geo.nearestOfficialKmOnSections(39.72, 47.20, [
            { coordinates: [[39.7, 47.2]], start_km: 0, finish_km: 10 },
            { coordinates: [[39.70, 47.20], [39.74, 47.20]], start_km: 5, finish_km: 5 },
        ])).toBeNull();
    });
});

describe('haversineKm', () => {
    test('известное расстояние между точками', () => {
        const km = geo.haversineKm(39.70, 47.20, 39.74, 47.20);
        expect(km).toBeCloseTo(3.0, 0);
    });
});
