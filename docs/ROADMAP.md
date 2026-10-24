# Дорожная карта проекта АЛЬБИОН.Локатор

> Версия: 1.3 · Дата: 2026-10-24 · Ветка: `feature/ui-variant-a`

## Этап 0 — Вход, пользователи и базовый порядок ✅

| # | Задача | Статус |
|---|---|---|
| 0.1 | Форма входа по ТЗ: «АЛЬБИОН.Локатор / закрытый веб-сервис / ...» | ✅ |
| 0.2 | Кнопка 👤 в статус-баре: окно аккаунта (смена логина/пароля, logout) | ✅ |
| 0.3 | Только админ создаёт пользователей; редактор не видит управление | ✅ |
| 0.4 | Принудительная смена пароля после бутстрапа и сброса админом | ✅ |
| 0.5 | Backup `users.db` скриптом + systemd timer | ✅ (`deploy/backup_data.py`) |
| 0.6 | `.github/workflows/qa.yml` + lockfile + merge в `main` | ⏳ workflow застрял из-за PAT scope |

## Этап 1 — Сеть: LAN + TLS + безопасность ✅

| # | Задача | Статус |
|---|---|---|
| 1.1 | `Secure` cookie (auto по `X-Forwarded-Proto`), CSRF double-submit, Origin-проверка, rate-limit, lockout, security headers, CSP, закрытие `/docs`/`/openapi.json` | ✅ |
| 1.2 | Reverse proxy nginx template, `ALBION_ALLOWED_ORIGINS`, порт 8021 loopback | ✅ (`deploy/nginx/`) |
| 1.3 | CloudPub tunnel: проверены X-Forwarded-Proto/Host/For/Real-IP | ✅ задокументировано |
| 1.4 | Ротация ключей Яндекса, `.env` 0600, preflight `deploy/security_check.py` | ✅ шаблон готов; физическая ротация ключей — ручная операция |

## Этап 2 — Данные и надёжность ✅

| # | Задача | Статус |
|---|---|---|
| 2.1 | Retry/backoff для WFS/Overpass/Yandex; TTL-кэш upstream | ✅ (`src/upstream.py`) |
| 2.2 | WFS paging (`startIndex`/`count`/`numberMatched`) + проверка дубликатов страниц | ✅ |
| 2.3 | МСК-61: 48 независимых контрольных векторов PROJ, тест, дисклеймер в UI | ✅ |
| 2.4 | JSON-структурированные логи, `request_id`, Prometheus `/metrics` | ✅ (`src/observability.py`) |
| 2.5 | Audit log: входы, смены данных, действия админа, импорт XLS | ✅ (`auth.audit_log`, `GET /api/audit`) |
| 2.6 | Backup офлайн-данных с manifest/checksum/integrity_check + rollback | ✅ (`deploy/backup_data.py`) |
| 2.7 | Скрипт обновления OSM PBF → index + MBTiles с integrity + rollback | ✅ (`deploy/update_offline_data.py`) |
| 2.8 | XLS/ODS-импорт установленных точек в SQLite | ✅ (`src/xls_import.py`) |
| 2.9 | Фильтры установленных точек: щиты, районы, трассы | ✅ |

## Этап 3 — Качество и поставка ⏳

| # | Задача | Приоритет |
|---|---|---|
| 3.1 | Merge `feature/ui-variant-a` → `main`, tagged release | **P1** |
| 3.2 | `pip-audit` в CI (проверка зависимостей Python) | **P2** |
| 3.3 | Офлайн-e2e в CI (vector tiles + local source, zero external requests) | **P2** |
| 3.4 | Нагрузочный тест: batch 50 файлов, 20 VU | **P3** |
| 3.5 | A11y/mobile audit обновление после статус-бара | **P2** |
| 3.6 | Версионированные релизы офлайн-данных (OSM snapshot tag + checksum) | **P3** |
| 3.7 | CI: `pip-audit` + Playwright + bun + backend + offline package | **P2** |

## Этап 4 — Расширение (после приёмки)

| # | Задача | Описание | Приоритет |
|---|---|---|---|
| 4.1 | Фильтры в workspace | Сохранение фильтров «Установленных» в персональном `user_preferences` вместо localStorage для синхронизации между устройствами | P2 |
| 4.2 | Пагинация API точек | `GET /api/installed?limit=200&offset=0&bbox=...` для больших наборов (>5000) | P3 |
| 4.3 | Map cluster для точек | При >2000 точек переключаться на `L.markerClusterGroup` вместо `L.featureGroup` | P3 |
| 4.4 | Официальные контрольные точки МСК-61 | Замена `msk61_proj_control_points.json` на кадастровые точки Росреестра после геодезической аттестации | P2 (блокировано внешней задачей) |
| 4.5 | ZeroTier / Windows VPN | Замена CloudPub на VPN-туннель (по решению заказчика) | P3 |
| 4.6 | Экспорт текущей точки | Экспорт результата поиска (точка + дорога + км + МСК) в XLS/CSV/PDF | P3 |

---
