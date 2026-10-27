# Albion Locator Autonom — документация проекта

**Актуально на:** 20.10.2026  
**Ветка:** `feature/ui-variant-a`  
**Активная сборка:** `src/main.py` + корневой `static/index.html`.  
Файлы `src/static/`, `release/`, `src/main (Copy*)`, `static/main.py` — архивные и не являются entry point.

Руководство по эксплуатации для пользователей (назначение кнопок и окон, настройки, типовые операции, скриншоты) — `/home/alexice/projects/albion-reports/locator/РУКОВОДСТВО_ПО_ЭКСПЛУАТАЦИИ_АЛЬБИОН_ЛОКАТОР.md` и PDF рядом.

## 1. Назначение

Локальное веб-приложение для работы с точками и дорогами Ростовской области:

- выбор точки на карте, по координатам, адресу или EXIF фотографии;
- поиск ближайшей дороги через СКДФ WFS или локальный SQLite-индекс;
- официальный километраж трассы по участкам `start_km`/`finish_km` СКДФ;
- предварительный калькулятор WGS84 ↔ МСК-61;
- персональные история, закладки, настройки и рабочая сессия;
- пакетная обработка фотографий;
- скриншоты карты.

Приложение ориентировано на локальный Linux-деплой и loopback/LAN. Полностью автономным оно не является: OSM-тайлы, СКДФ WFS и Yandex API требуют сети.

## 2. Технологический стек

| Слой | Технологии |
|---|---|
| Backend | Python 3.11+, FastAPI, Uvicorn, Pydantic, HTTPX |
| Auth/state | SQLite (`src/users.db`), PBKDF2-HMAC-SHA256, cookie-сессии |
| EXIF | Pillow, piexif |
| Frontend | HTML/CSS/Vanilla JS, Leaflet 1.9.4, dom-to-image-more |
| Android/desktop | не используется |
| Offline geo | SQLite, FTS5, RTree (`packages/offline_geo`) |
| Тесты | pytest, pytest subtests, Bun test, Playwright |
| Деплой | systemd user unit, порт 8021 |

## 3. Архитектура

```text
Браузер
  ├─ static/index.html: UI, Leaflet, inline JS
  ├─ /static/vendor/: Leaflet CSS/JS/images, dom-to-image-more
  └─ same-origin HTTP + cookie albion_session
          │
          ▼
FastAPI / Uvicorn (src/main.py)
  ├─ auth.py          — пользователи, сессии, preferences/workspace, bookmarks, history, installed_points, audit_log
  ├─ security.py      — CSRF/Origin, rate-limit, lockout, security headers, CSP
  ├─ observability.py — JSON-логи, X-Request-ID, /metrics
  ├─ upstream.py      — retry/backoff и TTL-кэш WFS/Overpass/Yandex
  ├─ xls_import.py    — XLS/XLSX/ODS → установленные точки
  ├─ batch.py         — пакетное чтение EXIF
  ├─ СКДФ WFS         — дорожная геометрия и официальные участки
  ├─ Yandex Geocoder  — адреса, дороги, reverse geocode
  ├─ offline_geo      — опциональный локальный индекс
  └─ MBTiles          — опциональные локальные тайлы
```

### Основные backend-модули

- `src/main.py` — FastAPI app, API-маршруты, WFS/Yandex proxy, геометрия, EXIF, статика, security middleware.
- `src/auth.py` — SQLite-схема, пользователи, сессии, PBKDF2, персональные настройки/workspace, закладки, история.
- `src/security.py` — CSRF double-submit, Origin, rate-limit/lockout, security headers и CSP, `Secure` cookie.
- `src/observability.py` — JSON-логи с `request_id`, доступ к `/metrics`, счётчики HTTP/upstream/кэша/аудита.
- `src/upstream.py` — общие retry/backoff и TTL-кэш для WFS, Overpass и Yandex.
- `src/xls_import.py` — разбор `.xls`/`.xlsx`/`.ods` (город, адрес, сторона, формат, координаты), конвертация через LibreOffice, устойчивость к битому `styles.xml`.
- `src/batch.py` — ограничения и пакетное извлечение GPS EXIF.
- `packages/offline_geo` — самостоятельный SQLite-индекс: адреса, улицы, дома, дороги, nearest road.
- `deploy/` — шаблон nginx с TLS, backup с manifest/checksum, атомарное обновление офлайн-данных, security preflight, создание администраторов (`create_default_admins.py`).

### Frontend

Один файл `static/index.html` без сборщика. Основные блоки:

- левый фрейм: вкладки **Точка / Пакетный поиск / Сохранённое**, карточка текущей точки;
- карта Leaflet;
- правый фрейм: стабильный блок «Источник СКДФ», официальный километраж;
- статус-бар: кнопки с подписями и разделителями — «Установленные» (меню фильтров и зелёный слой точек, активные режимы выводятся рядом), «Призрак», «Шаг», «Слежение», «Подписи», «Центр», «Автоскриншот», «Справка», «Пользователь», «Настройки», «Сброс»;
- единый блок поиска: сверху карты или в статус-баре (переключается в настройках);
- модальные настройки, справка, тосты.

## 4. Поиск и синхронизация

### Режимы поиска

- **Адрес** — Yandex SuggestView + серверный geocode; поле показывает определённый адрес.
- **Координаты** — по умолчанию WGS84; соответствующая зона МСК-61 определяется в фоне. Кнопка переключения предлагает только WGS84 и доступную МСК; при неверной зоне поле пустое. При активном призраке его координаты показываются в скобках.
- **Дороги** — подсказки по мере ввода, выбор трассы, автозагрузка участков СКДФ.

Чекбокс активации включает строку поиска. Он **не блокирует карту**. В активном режиме поиска блокируются пакетная загрузка и загрузка фото. После успешной загрузки фото поиск блокируется до «Очистить точку».

В пакетном режиме поиск недоступен, а постановка новых точек по карте отключена. Правый фрейм работает с активной пакетной точкой, выбранной кликом по маркеру на карте или по строке списка; выбранную точку можно двигать ползунком официального километража. Кнопка центрирования приближает карту к точке (zoom ≥16) или показывает все загруженные точки пакета.

### Официальный километраж

- Включён по умолчанию; галка рядом с полем снимается, чтобы отключить синхронизацию. Для дороги без кода участки СКДФ запрашиваются по названию; если официальных участков нет, ось строится по геометрии из ответа `locate`, а OSM/Overpass используется только если геометрии нет.
- При включении поле/ползунок заполняются км из СКДФ.
- Ползунок и поле двигают точку вдоль геометрии участка; `change`/Enter/«Перейти» фиксируют результат.
- Новая точка пересчитывает официальный км проекцией на участки.
- Разрывы и перекрытия участков обрабатываются явно; расхождения <50 м считаются стыком.
- Участки загружаются автоматически при наличии `road_code` у найденной дороги.
- Если в СКДФ нет официальных участков (например, `60К-31`), строится приблизительная ось по OSM Overpass: относительная шкала от 0 до геометрической длины, оранжевая пунктирная линия, клик по линии выбирает км и ставит точку. Данные помечаются как приблизительные.
- Дискретный шаг ползунка: 5/10/50/100/500 м. Шаг переключается кнопкой в статус-баре и селектором в настройках; под ползунком рисуются вертикальные штрихи выбранного шага и точки каждые 1000 м. Для длинных трасс, где шаг сливается, используется локальная шкала вокруг бегунка.
- Клавиатурное управление ползунком и управление колесом мыши включаются в настройках. Клавиатура взводится фокусом в поле км, Tab-фокусом/кликом по ползунку или кликом по нужному маркеру (призрак/красная точка); колесо работает при фокусе в поле км или на ползунке.
- Слежение за ползунком включается кнопкой 👁 в статус-баре или чекбоксом в настройках. При движении ползунка/вводе км карта следует за выбранной точкой (или призраком), сохраняя масштаб; клавиатурное управление картой на это время отключается, остаётся перетаскивание ЛКМ. Кнопка «Перейти» центрирует карту на выбранном километраже. Режим работает только при активном официальном километраже.

### Точка призрака

Кнопка 👻 в статус-баре. При активном километраже ползунок двигает зелёный клон, исходная красная точка остаётся. «Перейти» удаляет исходную точку и делает призрак красным. При повторном включении призрак сбрасывается к исходной точке, а не остаётся на прежнем месте. Карточка «Текущая точка» показывает координаты, МСК и источник активного призрака; после выключения возвращается к основной точке. Стрелки клавиатуры двигают ползунок после фокуса в поле км, Tab-фокуса/клика по ползунку или клика по маркеру: по зелёному призраку при включённом призраке, иначе — по красной точке.

## 5. Данные и хранение

| Данные | Расположение |
|---|---|
| Пользователи, сессии, preferences, workspace, закладки, история | `src/users.db` (`ALBION_USERS_DB`) |
| Настройки скриншота (без координат) | `src/user_settings.json` |
| Локальный геоиндекс | `ALBION_GEO_DATA_DIR/offline_geo.sqlite3` |
| Локальные тайлы | `ALBION_BASEMAP_MBTILES` |
| Векторные тайлы | `ALBION_VECTOR_MBTILES` (по умолчанию `$ALBION_GEO_DATA_DIR/vector.mbtiles`) |
| Граница области | `static/rostov_boundary.geojson` |
| Скриншоты серверного API | `src/screenshots/` |
| Скриншоты браузера | file picker / Downloads |
| Тема и часть UI-настроек | localStorage |

Общий `last_point` больше не используется. Позиция карты восстанавливается из персонального workspace.

## 6. Внешние интеграции

- **СКДФ WFS** `skdf_open:lyr_road_by_value` — ближайшая дорога и официальные участки. Имя слоя перебирается по списку кандидатов (`ALBION_WFS_ROAD_LAYER`, затем текущее и прежнее имя): поставщик переименовывает слои без предупреждения, и жёстко зашитое имя превращало установку точки в 502.
- **Yandex Geocoder** — адрес → координаты, reverse geocode, поиск дорог.
- **Yandex SuggestView** — подсказки адресов (JS API/Geosuggest keys).
- **OSM tiles** — подложка карты.
- **Overpass** — резервный контур области, если локальный GeoJSON недоступен.

Ключи Yandex хранятся в `.env`; client-visible ключи выдаются только после входа через `/api/maps-config`. Server-side `YANDEX_API_KEY` в браузер не передаётся.

## 7. API (кратко)

| Endpoint | Назначение |
|---|---|
| `GET /`, `/static/...` | UI и allowlist статики |
| `GET /api/health` | health-check |
| `POST /api/auth/login`, `/logout`; `GET /api/auth/me` | аутентификация |
| `/api/users...` | управление пользователями (admin) |
| `/api/installed...` | импорт XLS и установленные точки |
| `GET /api/audit` | журнал входов и действий (admin) |
| `GET /metrics` | Prometheus-метрики (loopback/токен) |
| `POST /api/locate` | ближайшая дорога (external/local) |
| `POST /api/roads/online/search` | поиск трасс/дорог |
| `POST /api/roads/online/sections` | официальные участки СКДФ (`code` или `name`) |
| `POST /api/roads/overpass/geometry` | приблизительная ось дороги по OSM |
| `POST /api/exif`, `/api/batch/upload` | GPS из фото |
| `POST /api/address/online/geocode`, `/api/geocode/reverse` | адрес ↔ координаты |
| `POST /api/msk61/from-geographic`, `/to-geographic` | МСК-61 |
| `/api/user/preferences`, `/api/user/workspace` | персональное состояние |
| `/api/bookmarks`, `/api/history` | закладки и история |
| `/api/screenshot-settings`, `/api/save-screenshot` | скриншоты |

Полная схема — `/docs` и `/openapi.json` запущенного приложения.

## 8. Конфигурация

Основные переменные:

| Переменная | Назначение |
|---|---|
| `ALBION_USERS_DB` | путь SQLite users/state |
| `ALBION_REQUIRE_PASSWORD_CHANGE` | требовать смену пароля после первого входа/сброса (по умолчанию `1`) |
| `ALBION_OFFLINE` | локальный источник дорог по умолчанию |
| `ALBION_GEO_DATA_DIR` | каталог offline index / MBTiles |
| `ALBION_BASEMAP_MBTILES` | явный путь растровых MBTiles |
| `ALBION_VECTOR_MBTILES` | явный путь векторных MBTiles (MapLibre) |
| `ALBION_DEFAULT_MAP_SOURCE` | `vector`/`local`/`external`; по умолчанию предпочитается установленная локальная подложка |
| `ALBION_WFS_ROAD_LAYER` | имя слоя СКДФ с дорогами; по умолчанию `skdf_open:lyr_road_by_value`, при переименовании у поставщика задаётся вручную |
| `ALBION_ALLOWED_ORIGINS` | дополнительные Origin |
| `ALBION_COOKIE_SECURE` | `auto`/`always`/`never` для session и CSRF cookie |
| `ALBION_CSRF` | double-submit CSRF (по умолчанию `1`) |
| `ALBION_ENABLE_DOCS` | открыть `/docs`, `/redoc`, `/openapi.json` (по умолчанию `0`) |
| `ALBION_CSP_POLICY` | переопределить Content-Security-Policy (`off` отключает) |
| `ALBION_API_RATE_LIMIT` | общий лимит `/api/*`; по умолчанию **0 (выключен)** — сервис работает локально/через VPN, а активная работа с ползунком даёт десятки запросов в минуту |
| `ALBION_LOGIN_RATE_LIMIT` | лимит попыток входа; остаётся включённым независимо от общего лимита |
| `ALBION_LOGIN_MAX_FAILURES` / `ALBION_LOGIN_LOCKOUT_SECONDS` | lockout после неудачных входов |
| `ALBION_LOG_FORMAT` / `ALBION_LOG_LEVEL` | JSON/plain logs и уровень |
| `ALBION_METRICS_TOKEN` | токен для `/metrics` через reverse proxy |
| `ALBION_UPSTREAM_RETRY_ATTEMPTS` / `ALBION_UPSTREAM_CACHE_TTL` | retry и TTL-кэш внешних API |
| `YANDEX_JS_API_KEY` | Yandex JS API |
| `YANDEX_SUGGEST_API_KEY` | Yandex SuggestView |
| `YANDEX_API_KEY` | server-side Geocoder/reverse |
| `YANDEX_STATIC_API_KEY` | резерв/не используется |
| `OLLAMA_HOST`, `OLLAMA_MODEL` | dormant helper, не в активном flow |

`.env` не коммитится; пример — `.env.example`.

## 9. Запуск и деплой

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m uvicorn main:app --app-dir src --host 127.0.0.1 --port 8021
```

Установленная конфигурация:

```bash
systemctl --user status albion-locator-autonom.service
systemctl --user restart albion-locator-autonom.service
journalctl --user -u albion-locator-autonom.service -f
```

Unit: `~/.config/systemd/user/albion-locator-autonom.service`, порт `8021`, lingering включён. Health: `curl http://127.0.0.1:8021/api/health`.

## 10. Тестирование и QA

```bash
# Backend/API
.venv/bin/pytest -q tests --ignore=tests/e2e

# Offline package
PYTHONPATH=packages/offline_geo/src .venv/bin/pytest -q packages/offline_geo/tests

# Frontend unit
bun test tests/js

# Browser E2E
uv pip install --python .venv/bin/python -r requirements-e2e.txt
.venv/bin/playwright install chromium
.venv/bin/pytest -q tests/e2e
```

Текущие результаты (24.10.2026): Python **140 passed** (+**53** теста офлайн-пакета), Bun **30 passed**, Playwright **71 passed**, axe-core **0 violations**.

Подготовка E2E-сервера: `tests/e2e/conftest.py` создаёт изолированную SQLite и настройки, отключает rate limit и очищает пользовательскую сессию/`workspace` перед каждым тестом — иначе восстановление состояния из прошлого теста гоняется с его assert'ами.

QA-документы:

- `docs/qa-agent-improvement-plan.md` — план и статус;
- `docs/performance-nearest-road.md` — RTree/benchmark;
- `docs/performance-runtime.md` — память/CPU;
- `docs/accessibility-audit.md` — axe;
- `docs/provider-live-check.md` — live Yandex/WFS;
- `docs/CHANGES-2026-10-27.md` — последние изменения: исправление 502 при установке точки;
- `docs/CHANGES-2026-10-24.md`, `docs/CHANGES-2026-10-20.md` — предыдущие срезы.

## 11. Безопасность

- Все `/api/*`, кроме health/login, требуют cookie-сессию.
- Cookie HttpOnly, SameSite=Lax, TTL 30 дней; `Secure` включается по HTTPS/`X-Forwarded-Proto` (`ALBION_COOKIE_SECURE=auto|always|never`).
- Mutating API проверяет Origin и double-submit CSRF: cookie `albion_csrf` + `X-CSRF-Token`; принудительная смена пароля не обходит проверку.
- Общий rate-limit `/api/*` по умолчанию выключен (`ALBION_API_RATE_LIMIT=0`): контур локальный/через VPN. Для публикации наружу включается явно. Лимит входов и lockout (5 неудач → 15 минут) работают всегда.
- `/docs`, `/redoc`, `/openapi.json` закрыты по умолчанию (`ALBION_ENABLE_DOCS=1` для разработки).
- Security headers: CSP, HSTS на HTTPS, X-Frame-Options DENY, nosniff, Referrer/Permissions Policy.
- Пароли — PBKDF2-HMAC-SHA256 с солью; сессионные токены хранятся в БД хэшами; audit log фиксирует входы и действия с пользователями.
- Учётные записи по умолчанию: `init_db()` создаёт `админ` / `альбион`; `deploy/create_default_admins.py` добавляет равноправных администраторов (`техотдел`, `админ`, `администратор`, `альбион`) и идемпотентно переопределяет пароли. Пароли по умолчанию меняются до публикации сервиса.
- Загрузки ограничены по размеру/формату; PNG валидируется; путь сохранения фиксирован.
- Статика отдаётся по allowlist, а не всем каталогом.
- Перед публикацией: `.env` 0600, TLS/reverse proxy или туннель с `X-Forwarded-Proto`, ротация и доменные ограничения ключей Яндекса; шаблон nginx и preflight — `deploy/README.md`.

## 12. Известные ограничения

- OSM-тайлы, СКДФ WFS и Yandex API требуют сети и квот.
- Локальный региональный SQLite index/MBTiles в репозиторий не входят.
- МСК-61 — предварительный расчёт; требуется официальная сверка.
- Официальный километраж зависит от полноты и качества слоя СКДФ.
- Приблизительная ось OSM зависит от полноты OSM Overpass, требует сети (первый запрос может занимать десятки секунд) и даёт относительный километраж, а не официальный.
- Workflow `.github/workflows/qa.yml` подготовлен, но для публикации нужен PAT со scope `workflow`.
- Полная ручная screen-reader/контрастная проверка остаётся отдельным шагом.

## 13. Диагностика

| Симптом | Что проверить |
|---|---|
| Страница не грузится | `systemctl --user status`, `/api/health`, DevTools Console |
| Карта серая | OSM-тайлы / сеть; локальные тайлы — `ALBION_BASEMAP_MBTILES` |
| Нет подсказок адреса | `YANDEX_JS_API_KEY`, `YANDEX_SUGGEST_API_KEY`, `/api/maps-config` |
| Нет километража | код дороги, `/api/roads/online/sections`, `/api/roads/overpass/geometry` (по коду или названию) |
| Пустой локальный поиск | `ALBION_GEO_DATA_DIR/offline_geo.sqlite3` |
| Старое поведение UI | жёсткая перезагрузка `Ctrl+Shift+R`; статика отдаётся с `no-store` |
