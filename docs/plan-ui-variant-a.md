# План реализации интерфейса «Вариант A» — АЛЬБИОН: Локатор

**Для передачи агенту-исполнителю.** Документ описывает поэтапную переработку текущего приложения без переписывания GIS-логики и без изменения существующей ветки. Это план, не кодовая спецификация: ключевые решения, отмеченные как «рекомендуется», надо проверить с владельцем до фазы, где меняются данные/контракт API.

## 0. Цель, текущая основа и ограничения

Цель — перевести текущий экран на вариант A: два постоянно видимых фрейма и карта между ними, компактные вкладки в левом фрейме, результаты/текущая точка/официальный километраж справа, короткий статус-бар снизу и редкие действия в настройках. Сохранить действующие сценарии и источники данных, явно разделить «черновик поиска» и «точку, уже поставленную на карту».

### Проверенная основа

- Клиент — один большой `static/index.html` (~3270 строк): inline CSS/JS, Leaflet, без frontend build system. Сохранять текущий стек; не добавлять React/SPA/build pipeline в рамках UI-редизайна.
- Backend — FastAPI в `src/main.py`, модуль пакетной загрузки — `src/batch.py`, пользователи/сессии — SQLite-модуль `src/auth.py`, локальный геоиндекс — пакет `packages/offline_geo`.
- Данные: фото EXIF — `/api/exif`, пакет EXIF — `/api/batch/upload`, поиск ближайшей дороги — `/api/locate`, адресные подсказки — `/api/address/suggest` (локальный индекс) и `/api/address/online/suggest`, forward-geocode — `/api/address/online/geocode`, дороги — `/api/roads/online/search`, километраж — `/api/roads/online/sections`, МСК — `/api/msk61/{to-geographic,from-geographic}`, закладки — `/api/bookmarks`, настройки скриншота — `/api/screenshot-settings`.
- Существующие интерактивные элементы: `#sidebar-left`, `#sidebar-right`, `#map`, `#resultsFrame`, `#searchModeSelect` и панели `#addressSearchPanel`, `#coordsSearchPanel`, `#roadSearchPanel`, `#mskSearchPanel`; ползунок `#roadKmSlider`; пакетная панель `#batch-panel`.
- Сейчас поиск по карте вызывается на `dblclick` (`map.on('dblclick')` около строки 1317), обычный `click` только скрывает результаты. Адрес/дорога из SuggestView вызывают `selectPoint()` сразу, то есть ставят маркер автоматически — это надо изменить согласно требованию «только кнопка Поставить точку».
- `selectPoint()` около строки 2798 удаляет прежние маркеры, показывает координаты и запускает `/api/locate`; `locate()` около 2870 записывает историю и отображает результат. Ползунок (`input`/`change`, около 1958–1969) обновляет число на лету, ставит точку после отпускания.
- Настройки сейчас частично общие: `src/main.py` пишет `src/user_settings.json` (`last_point`, screenshot settings); закладки — общий `src/bookmarks.json`; история/тема/режим поиска/источники — `localStorage`. Текущая схема не обеспечивает обещанное восстановление состояния отдельно для каждого пользователя.
- Авторизация на cookie с SQLite уже есть. Middleware `SessionAuthMiddleware` защищает `/api/*`, кроме health/login. Роли admin/editor.
- `/api/address/suggest` — иерархический локальный поиск (settlement → street → house) из индекса. Yandex SuggestView сейчас — один адресный инпут. Прямого reverse-geocoding API (координаты → адрес), lookup муниципалитета и слоя кадастровых границ для определения зоны МСК здесь не найдено.
- Пакетный API уже ограничивает размер/количество фото и возвращает только filename/lat/lon/error. UI уже отображает список и группу маркеров, но отдельные названия/удаление/перемещение/серверное восстановление не реализованы.

Ссылки на вероятные точки работ (после изменения файлов исполнитель должен повторно проверить строки): `static/index.html:28–185` layout/CSS; `:540–690` левый фрейм; `:799–919` правый и настройки; `:957–1170` панели/история/закладки; `:1317–1324` события карты; `:1326–1400` screenshot settings; `:1492–1710` подсказки; `:1744–1969` дорожный chainage; `:2113–2203` перенос панелей и модалка настроек; `:2690–2919` результат/копирование/точка; `:3018–3270` batch. Backend anchors: `src/main.py:806–978` модели/файловые настройки; `:1090–1101` screenshot-settings; `:1165–1243` locate; `:1331–1506` адресный поиск; `:1545–1685` дороги/участки; `:1703–1736` МСК; `:1759–1779` bookmarks; `src/auth.py:41–67` схема SQLite; `src/batch.py:14–73` ограничения и EXIF.

## 1. UX-структура после редизайна

### 1.1 Общая компоновка

- Основная область: **левый фрейм | карта | правый фрейм**. Сохранить текущие collapse controls и адаптацию Leaflet (`map.invalidateSize()` после изменения ширины).
- Высоты внутренних списков/панелей рассчитывать через flex (`min-height:0`, скролл внутри вкладки), а не за счёт общего скролла страницы. Нижний статус-бар — фиксированной небольшой высоты, не перекрывает карту.
- На узких экранах использовать уже имеющийся сценарий «открыт один фрейм за раз»; статус-бар становится компактным, текстовые сегменты сворачиваются до иконок с `title`/доступными именами.
- Удалить/перепрофилировать плавающий `#map-search-toolbar`, чтобы не было второго независимого UI поиска поверх карты. Не клонировать поля: DOM-элементы подсказок должны иметь ровно одного владельца и одну инициализацию SuggestView.

### 1.2 Левый фрейм — три вкладки

Вкладки: **Точка**, **Пакетный поиск**, **Сохранённое**. Вкладки переключают панели, но не очищают форму, выбранную точку, список пакетных точек, список закладок, карту или результат справа.

#### Точка

1. EXIF dropzone/кнопка выбора фото остаются вверху; одиночная загрузка сохраняет текущий поток EXIF → кандидат → явное подтверждение постановки.
2. Убрать кнопку Ростовской области, кнопку Справка и отдельный блок ввода `coordSingle`. Удалить их старые DOM/обработчики после миграции; функции региона и справки подключить к status bar.
3. Чекбокс **«Режим поиска»** (точный текст согласовать) рядом с взаимоисключающими режимами-кнопками **Адрес / Координаты / Дороги**. Радиогруппа обязательно доступна с клавиатуры; при смене режима очищать/скрывать лишь несовместимые подсказки, не коммитить точку.
4. При выключенном чекбоксе поля продолжают работать как калькулятор/подготовка черновика, но не блокируют обычную карту. При включённом режиме поиск захватывает выбор: обычный map-click не должен создавать/перемещать точку, официальный км не должен менять активную точку. Панорамирование карты/zoom можно оставить активными.
5. Внизу — явная кнопка **«Поставить точку»**. Никакая подсказка/blur/Enter не должна сама коммитить точку. Нажатие валидирует черновик, проверяет координаты/регион, подтверждает точку, выключает режим блокировки (либо переводит в состояние «точка подтверждена»), обновляет правый фрейм/карту/статус и синхронизацию.
6. **Адрес:** первое поле — город/сельский населённый пункт, второе — улица/дом, второе отключено до выбора первого. Подсказки выбирать только из объектов Ростовской области. Для local index использовать `suggest_context(q, settlement=..., street=...)`; online flow требуемой иерархии — проверить API Yandex. На каждый выбранный объект показывать источник и точность; выбор — только кандидат, не `selectPoint()`.
7. **Координаты:** два синхронизированных инпута: WGS84 и координаты проекции МСК-61. Слева предпочтительно десятичные градусы `широта, долгота`; опционально принимать DMS, например `47°14′42.2″N 39°40′03.2″E`. Справа — X (север), Y (восток) в метрах. Рядом циклический селектор CRS: МСК-61 зона 1 → зона 2 → зона 3 → GPS/WGS84 → повтор. Это четыре состояния, хотя в требованиях упомянуто «3 последовательных состояния»; в UI надёжнее вывести текущую систему текстом, а не только значком.
8. Исправить/заменить `parseCoords()`: текущий парсер не поддерживает корректно DMS и отрицательные направления S/W, а удаление символов градуса/минут может интерпретировать компоненты DMS как первые два десятичных числа. Создать покрытый тестами parser для decimal lat/lon и DMS с N/S/E/W, знаков, запятых, пробелов, диапазона и malformed input.
9. Координатные поля синхронизировать на `change`/потере фокуса (и опционально debounce preview), не на каждый символ: при WGS update запросить `POST /api/msk61/from-geographic`, при МСК — `/api/msk61/to-geographic`; использовать revision/AbortController, чтобы поздний ответ не перезаписал новый ввод. Не запускать road search на каждом conversion.
10. Фильтр области: перед commit проверять lat/lon по достоверной границе, не только bounding box. Backend уже имеет `_point_in_rostov()` и GeoJSON; учесть границу polygon/holes и ответить явным предупреждением. Внешняя геокодировка ограничивает bbox, но эту проверку необходимо делать и для manual WGS/DMS и МСК-конверсии.
11. **Дороги:** поле 1 — название/код дороги и подсказки только трасс/дорог; поле 2 — официальный километраж/варианты километров участка. При выборе дороги загрузить/подготовить `roadKmState` и показать рекомендуемый ближайший км как черновик. Не фиксировать точку автоматически; кнопка «Поставить точку» коммитит координаты км.
12. Нормализация латиницы: требование, скорее всего, про клавиатурную раскладку («ghbdtn» → «привет»), не обычную транслитерацию («privet»). Реализовать отдельную pure-функцию `normalizeSearchQuery(value, mode)`, сохранять исходный текст, не повреждать код дороги/официальные латинские названия, давать понятную индикацию автокоррекции. По возможности предлагать корректировку, а не заменять поле незаметно. Валидация только русского текста не должна запрещать цифры, дефисы и обязательные коды дорог `M-4`/`R-280`.

#### Пакетный поиск

Перенести существующую пакетную загрузку из бокового overlay `#batch-panel` в одноимённую вкладку левого фрейма. Сохранить multi-file drag/drop, JPEG/TIFF/EXIF и лимиты API. Вкладка показывает прогресс, ошибки по файлам, список объектов и controls:

- каждой точке — редактируемое имя (`Точка 1`, `Точка 2`…), WGS координаты, выбор строки, редактирование имени, удаление;
- чекбокс/кнопка **«Показать все на карте»**; в выключенном состоянии маркеры не отображаются до выбора строки (рекомендуемое поведение, требует UX подтверждения);
- кнопка **«Очистить список»** очищает пакетные маркеры, строки, активную batch-точку, активный результат, но не историю/избранное и не одиночную точку без подтверждения, если они независимы;
- сохранение выбранной batch-точки в избранное через стандартную кнопку в правом фрейме.

**Редактирование расположения — рекомендуемая схема:** явная кнопка/иконка «Редактировать точку» в строке → один маркер становится зелёным и draggable, остальные маркеры становятся неинтерактивными; пан/zoom карты остаются доступны; `dragend` обновляет preview, кнопки «Сохранить»/«Отмена» коммитят/откатывают координаты. Это безопаснее, чем два значения обычного map-click (первый выбор, следующий перенос, третий фиксация), которые легко спутать с dblclick и обычной навигацией. Если владелец выберет click-to-move, сделать отдельное явное состояние «Выберите новое место» с одним перехваченным click и отдельным «Подтвердить»; без active edit никакие клики карты не меняют batch-точку.

Переиспользовать API `batch_upload`; изменить фронтенд-модель точки на стабильный `clientId`, name, filename, lat/lon, state, optional locate result. Backend не обязан хранить временные фото после извлечения EXIF. Ограничить concurrent requests и не выполнять `/api/locate` для всех N точек автоматически.

#### Сохранённое

Единая вкладка с переключением **Избранное / История** (или два внутренних списка с под-вкладками), высота списка — остаток фрейма, список прокручивается внутри. Закладка: имя можно редактировать inline, click ставит точку на карте; добавить delete и подтверждение удаления при необходимости. История: координаты/время/источник; выбор восстанавливает координату, дорогу пересчитывает. Текущие `/api/bookmarks` общие для всех пользователей и JSON-файл — перевести на per-user storage (см. persistence).

### 1.3 Правый фрейм

- Удалить верхнюю шестерёнку. Верхняя полоса сохраняет сворачивание фрейма и название **Точка / Дорога** либо компактный контекст активного объекта.
- Удалить из него «быстрые параметры поиска»; радиус и фильтр основных дорог живут в настройках.
- **Карточка текущей точки** — всегда на виду, но до выбора показывать пустое/нейтральное состояние: «Выберите точку на карте или в левом фрейме». Поля: WGS84 (decimal и/или DMS), МСК (если рассчитано, с явно отмеченным предварительным профилем), адрес (если reverse geocoder даст результат с источником), муниципальный район/округ (только если доступен достоверный polygon lookup). Рядом у каждого доступного значения — кнопка копирования. Отдельная звезда сохраняет точку/название в Избранное.
- **Источник и результат ближайшей дороги**: использовать данные `/api/locate`; источник показывать всегда (`СКДФ WFS`/локальная база). По адресу — отдельно показать выбранный адрес и источник геокодера, а nearest road — отдельным результатом; не подменять адрес ближайшей дорогой. По ручным координатам/клику/EXIF — показывать ближайшую дорожную сущность, если найдена. По режиму дорог — выбранную трассу и официальный chainage.
- Добавить общие действия **Скопировать** и **Скриншот** в карточку справа. Копирование использует один публичный serializer, совместимый по тексту с `window.currentRoadCopyText`/`copyRoadInfo()` (сначала зафиксировать текущий формат тестом). До допустимой точки/данных действия disabled. Скриншот пока вызывает существующий `startScreenshotMode()`; отдельное изменение поведения и визуальных слоёв скриншота — отдельная задача.
- Оставить официальный км, поле, «Перейти», slider и диапазон. Когда активен режим поиска/черновика, отключить controls, но сохранить отображение committed value; после подтверждения синхронизировать с новой точкой. При slider `input` обновлять визуальный preview и WGS/MСК-поля, не запускать много сетевых locate-запросов; на `change`/release — commit один раз и вызвать поиск.
- Если координатный режим активен, slider синхронно обновляет координатные поля. Если активен режим «Дороги», slider обновляет поле 2 и карту. Для режима «Адрес» координаты выбранной точки также обновляются, но исходный адрес сохраняется как описание источника.

### 1.4 Нижний статус-бар

Горизонтальная панель с `aria-label` и адаптивными элементами. Порядок слева направо в рабочем макете (требование перечисляло справа-налево):

- слева — кнопка копировать текущую строку + текст текущего объекта. Приоритет: `Трасса + официальный км` → выбранный `Адрес` → `WGS84 lat, lon`. Текст, copy и скриншотное имя должны формироваться из одной модели состояния;
- справа — быстрый переход/центрирование на Ростовскую область (значок мишени; не геолокация пользователя), справка `?`, автоскриншот и задержка, настройки ⚙;
- иконка «Очистить рабочую сессию» только после отдельного подтверждения. Не использовать один значок reset для удаления закладок/настроек без пояснения.
- Сегменты с текущим состоянием (автоскрин включён/выключен, задержка), `title`, клавиатурной доступностью, видимым focus. На малой ширине текст текущей точки обрезается с доступным полным значением.

### 1.5 Настройки в ⚙️ модальном окне

1. **Параметры поиска дороги:** одна строка `радиус (м)` + чекбокс «Только основные дороги» (на очень узком экране переносится на 2 строки). Валидация диапазона — текущие 10–5000 м.
2. **Источники дорожных данных:** оставить текущие источники `external/local` и карты `OSM/MBTiles` с существующими статусами availability; добавить выбор вида/источника контура области. Не сбрасывать выбор silently при отсутствии локальных данных.
3. **Автоскриншот:** задержка/включение как сейчас; путь остается серверным, read-only/информационным.
4. **Мой аккаунт**.
5. **Пользователи** (admin only).
6. **Настройки по умолчанию/интерфейса:** тема, предпочтительный режим/CRS, тексты справки (формат/роль владельца уточнить), начальные значения радиуса/фильтра, возможно статус-бар.
7. Все сохранения автоматические: debounce с индикатором «Сохранено/Сохраняю/Ошибка»; для account/user mutations кнопки submit остаются, т.к. это чувствительные операции, они не должны отправляться на каждый ввод.
8. При закрытии модалки сохранить настройки; не переиспользовать «Сохранить все настройки», если авто-сохранение действительно включено. Ошибку API показывать и оставлять UI в состоянии retryable.

## 2. Архитектура состояния и контракты

### 2.1 Единая модель UI

До массового перемещения DOM завести один владелец состояния (в текущем стеке — plain JS object + функции/события, без библиотеки). Развести:

- `workspace`: активная вкладка слева, свернутость левого/правого фрейма, центр карты/zoom, видимость контуров/слоёв;
- `searchDraft`: checkbox armed, mode, сырые значения полей, выбранные address/road suggestion IDs, parsed WGS, optional converted CRS, validation/error, source/provenance;
- `activePoint`: committed lat/lon, source (`map`, `photo`, `address`, `coords`, `road-km`, `bookmark`, `batch`), name/address/CRS, revision;
- `roadResult`: `/api/locate` result, official sections/road km state, nearest route, source;
- `batch`: points collection, active id, edit mode, show-all flag;
- `preferences`: per-user search settings and display defaults.

Не считать Leaflet marker единственным источником координат: синхронизировать activePoint ↔ marker ↔ form fields через `setDraft`, `commitPoint`, `renderPoint`, `renderRoadResult`; Leaflet layers — отображение модели.

### 2.2 Состояния блокировки

Рекомендуемый конечный автомат:

- `idle`: обычная карта/вкладка; map interaction не делает неожиданный commit (оставить текущий dblclick только если владелец сохраняет старый сценарий; иначе сделать отдельный явный click-to-select режим).
- `search-armed`: чекбокс включён; map click не ставит/не двигает активную точку; km slider и поле км отключены для commit; поля/подсказки редактируют draft.
- `draft-ready`: valid selected object/coordinate; карта показывает preview-кандидат (визуально отличный от committed marker) при желании; адрес/дорога/координата не сохранены в history.
- `committed`: нажатие «Поставить точку» создает activePoint; запускает единый refresh road result, MСК/address display и синхронизацию полей; разблокирует slider согласно договоренному поведению.
- `batch-edit`: активен ровно один batch marker; остальные markers non-interactive; map normal-click не создает обычную точку.

Контракт события: только `commitPoint()` пишет историю/сессию и вызывает `/api/locate`. Поиск/конвертация/preview не пишут историю. Защитить асинхронные вызовы sequence ID или `AbortController`, чтобы поздний locate/geocode response старой точки не переписал актуальную.

### 2.3 Пер-user persistence

Цель «после следующего входа всё как было» требует backend, а не только `localStorage`.

Рекомендуемый подход — расширить `src/auth.py` schema/migration SQLite (тот же `ALBION_USERS_DB`) таблицами с `user_id` FK:

- `user_preferences(user_id PRIMARY KEY, payload_json, updated_at, version)` — настройки, тема и значения по умолчанию;
- `user_workspace(user_id PRIMARY KEY, payload_json, updated_at, version)` — карта, панель/вкладка, draft/active point, batch rows, прочее восстановимое состояние;
- `user_bookmarks(id PRIMARY KEY, user_id, name, lat, lon, metadata_json, created_at, updated_at)` — персональные избранные точки (вместо общего JSON).
- Историю оставить ограниченным кольцевым буфером в workspace payload или отдельной таблице максимум N=100, согласовать размер; не превращать историю в бесконечный журнал.
- API новые: `GET/PUT/DELETE /api/user/preferences`, `GET/PUT/DELETE /api/user/workspace`. Существующие bookmark routes сделать per-user, тестировать доступ между пользователями. Проверить, что middleware везде дает `request.state.user`; нельзя принимать `user_id` из body/query как authority.
- Validate JSON Pydantic: размер payload, структура, finite lat/lon, CRS whitelist, диапазоны zoom/radius, ограничение batch элементов и длин названий/строк. SQLite UPSERT + transaction, optimistic `version`/updated_at для двух вкладок; autosave debounce 300–800 мс, финальная отправка при `visibilitychange` best-effort. Ошибка сохранения видима.
- Старые `localStorage` значения мигрировать на клиенте после входа (тема, search mode, data/map source, history). Уважать уже сохраненное серверное значение; не затирать его значениями по умолчанию при первом GET. При успешной миграции записать marker версии и старые ключи пока не удалять автоматически.
- Текущие `user_settings.json` и `bookmarks.json` сохранить как резервную копию/источник однократного импорта. Не удалять JSON и не менять владельца существующих bookmarks без согласованной политики. Рекомендация: импортировать существующие общие bookmarks одному admin-аккаунту и сделать архив копий; подтвердить это с владельцем.
- `src/main.py:1172–1175` и offline counterpart `:1292–1297` сейчас пишут `last_point` в глобальный settings файл при каждом locate; удалить из общего screenshot config или перейти на user workspace save. Нельзя перетирать состояние другого пользователя.
- `src/main.py:1090–1101` screenshot settings сейчас общий `load_settings()` — решить, какие значения per-user (delay/autoscreenshot) и какие instance-level (server save path/source availability). Разделить; секреты/ключи API остаются серверными env, не слать на клиента сверх текущего maps-config.

«Очистить сессию» — рекомендуемое действие очищает только workspace/draft/history-map position и возвращает map center/zoom/panel defaults; не удаляет избранное или server-wide data. Если владелец действительно хочет сбросить ещё preferences/bookmarks, сделать отдельный подтверждённый destructive action с явным перечнем; не связывать с обычной очисткой.

## 3. Найденные риски/изъяны текущей логики — поправить или покрыть тестами

1. **Карта:** одиночный click сейчас не ставит точку; dblclick ставит, click лишь скрывает `#resultsFrame`. Зафиксировать пользовательский контракт до изменения; автоматическая переделка на click может конфликтовать с pan/dblclick и batch editing.
2. **Автоматический commit:** Yandex address/road suggestion сейчас вызывает `selectPoint()` внутри `resolveOnlineAddress`/`resolveOnlineRoad`; противоречит новому явному подтверждению. Разделить selection/search result и commit.
3. **Координатный формат:** текущий `parseCoords()` теряет семантику DMS, направлений S/W и может неправильно распарсить DMS; не расширять поверх него без тестов.
4. **Chainage snapping:** `nearestOfficialKm()` около строки 1843 сравнивает точку только с вершинами линии, а не ближайшей точкой сегмента. `distanceToSectionKm()` также сравнивает вершины. Поэтому ближайший км к произвольной координате может быть заметно неточным при редких вершинах. Исправить геометрию через projection на каждый сегмент, вычислить долю длины линии, затем переводить в официальный `start_km..finish_km`; учитывать направление, gaps, overlapping/старые alignment sections и точность единиц.
5. **Неэквивалентные расстояния:** `buildRoadKmState()` задает slider `totalLengthKm = lastKm-firstKm`, но геометрические длины участков и диапазон chainage могут не совпасть. В разрывах между участками `sectionForOfficialKm()` возвращает null. Slider должен быть либо по cumulative geometric chainage с отдельной таблицей official km, либо использовать валидные chainage intervals с gap-aware UI; это отдельный acceptance item.
6. **Race conditions:** address/road suggester имеют sequence guards, а общий `locate()` не выглядит защищенным от медленного ответа по старой точке. Добавить point revision guard до render и записи history.
7. **Область:** Yandex geocode проверяет bbox/наличие `province/area=Ростовская область`; пользовательский ввод и МСК результаты должны проверяться по точному полигону. `_point_in_rostov()` основан на outer rings и может не учитывать holes/multipart semantics полностью; дописать tests на boundary/точки вне региона.
8. **Адресная модель:** direct reverse-geocoding отсутствует, поэтому адрес выбранной координаты не гарантирован. Не показывать выдуманный адрес или муниципалитет. Сначала определить провайдера/API/лицензию, privacy implications и fallback; не отправлять точные координаты во внешний сервис молча.
9. **МСК auto-zone:** auto-zone не делать по одному longitude или текущей позиции без официального кадастрового zone polygon/authoritative source. Пока слоя нет — требовать ручной выбор или показывать «зона не подтверждена». Известное преобразование сейчас помечается предварительным (`requires_confirmation`); сохранить предупреждение.
10. **Избранное/настройки/last point:** JSON-файлы и localStorage сейчас shared/per-browser, не per-user; перед вводом новой схемы сделать безопасный import/backup, тесты ownership.
11. **Batch UI:** существующие batch markers не draggable; batch list не имеет individual rename/delete; `selectBatchPoint` только центрует/открывает popup, не синхронизирует общий right pane; `clearBatchPoints` очищает layer + list. Реализовать явную state machine edit и проверять изоляцию обычной карты.
12. **Auto settings:** `loadScreenshotSettings()` использует `data.delay || 3`, поэтому значение `0` (серверная модель допускает 0) заменяется на 3. Исправить на nullish/default check и тестировать граничные значения.
13. **Контур:** `loadLocalRostovBoundary()` выставляет checkbox/screenshotSettings в `true` при успешной загрузке; если checkbox — выбор видимости, загрузка и видимость должны стать разными понятиями, иначе сохраненный выключенный флаг будет перезаписан.
14. **Тесты static:** `tests/test_auth.py::test_root_serves_login_gated_app_without_caching` проверяет presence IDs и fragment text. При удалении старых элементов обновить assertions на новую структуру/IDs/безопасные legacy contracts, не просто ослабить проверку.

## 4. Этапы реализации

### Этап 0 — Защита текущей версии и принятие решений

**Не менять код, пока не установлен чистый базис ветки.** В рабочей папке уже есть незакоммиченные правки в `README.md`, `TECHNICAL_DESCRIPTION.md`, `src/main.py`, `static/index.html`, `tests/test_auth.py`, `tests/test_main.py`, и добавленные `design/`, `docs/`. Они не должны потеряться или случайно попасть в UI feature branch без ревью.

1. Снять `git status --short --branch`, текущий commit и remote. Не делать reset/checkout с потерей изменений.
2. Уточнить, должны ли незакоммиченные изменения по offline geo/address/MSK войти в новый branch: сохранить их через отдельную checkpoint ветку/commit после review либо оставить в исходной рабочей копии и создать чистый worktree от базового commit.
3. Рекомендуемая схема: GitHub repo/текущая ветка остаются нетронутыми; отдельный `feature/ui-variant-a` создаётся от согласованной базы после фиксации текущей разработки, PR идет в исходный репозиторий. Если под «форк» имеется в виду отдельный GitHub repository, это отдельный выбор; не делать push или fork без явного согласования.
4. Зафиксировать решения: single click vs dblclick; текущий provider reverse geocode; миграция общих bookmarks; reset semantics; поведение batch list без «Показать всё»; статус определения района/МСК зоны.
5. Выполнить baseline `pytest` и записать результат до UI изменений; проверить offline_geo package tests отдельно, если проектный workflow их запускает.

**Выход:** чистый изолированный рабочий branch/worktree, тестовый baseline, согласованные решения, резервная копия пользовательских JSON/SQLite.

### Этап 1 — Спецификация state/UX и контрактов

1. Утвердить DOM/state diagram, клавиатурные состояния и ширину фреймов/точки breakpoint.
2. Описать `activePoint`/`searchDraft`/`roadResult`/`batchPoint` JSON и переходы (см. §2.1–2.2).
3. Описать shape API prefs/workspace/bookmark и валидацию; зафиксировать формат clipboard и источники данных.
4. Определить stale response правила, slider preview vs commit и точки записи history.

**Выход:** короткий ADR/schema в плане или типизированные примеры в test fixtures.

### Этап 2 — Стабилизация гео-ядра до UI

1. Написать unit tests для decimal/DMS parser (N/S/E/W, lat/lon order, malformed/out-of-range, decimal comma), keyboard-layout normalizer и serializers.
2. Реализовать pure functions без UI: coordinate parse/format, request revision guards, clipboard formatter, chainage projection/snap.
3. Исправить nearest point projection на сегмент, official km calculation на sections, gaps/overlaps and slider mapping. Тестировать искусственные LineString/MultiLineString fixtures с редкими точками, обратным direction и gap.
4. Добавить unit tests для point-in-rostov boundary including exact boundary, outside bounding-box but inside? multi polygon/hole; если достоверной геометрии нет — серверная fail-closed response.
5. Проверить API различия external/local response; унифицировать source/provenance, не обещать `side` в offline данных там, где она неизвестна.

**Выход:** тестируемое гео-ядро и определённые semantics координат/км; UI пока прежний.

### Этап 3 — Backend persistence per-user

1. Добавить версионируемые SQLite migration в `src/auth.py` с FK и per-user tables (preferences/workspace/bookmarks); безопасно запускать несколько раз.
2. Добавить Pydantic schema и authenticated GET/PUT/DELETE endpoints в `src/main.py`; использовать текущую сессию, транзакции/UPSERT, payload limits, optimistic version.
3. Перевести `/api/bookmarks` на user scoping; авторизация/изоляция обязательны на list/create/update/delete.
4. Разделить per-user screenshot/UI prefs от server-wide source configuration. Не выдавать путь/конфиденциальные конфиги сверх существующего поведения.
5. Сделать migration tool или одноразовый импорт JSON/localStorage с backup, version flag, idempotence и rollback. Не удалять старые файлы.
6. Проверить CSRF/CORS middleware для всех новых mutating routes и роли доступа.

**Выход:** API и миграции, backend tests; фронтенд может продолжить использовать localStorage до этапа 6.

### Этап 4 — Перестройка каркаса и доступности

1. В `static/index.html` заменить текущий search toolbar/routing на новый основной layout: left tabs, right current point/result/kilometer, bottom status bar; settings source fields переместить через существующий Settings modal без дублирования.
2. Перенести пакетную панель в tab; удалить старые buttons/overlay только после переноса всех обработчиков/refs.
3. Сохранить стабильные IDs на время миграции или обновить все refs/search assertions одной транзакцией; использовать `data-*`/понятные IDs, никаких duplicate IDs при перемещении DOM.
4. Добавить семантику `role=tablist/tab/tabpanel`, `aria-selected`, tab keyboard navigation, button labels и focus management модалок; avoid div as controls.
5. Оптимизировать flex/scroll/compact responsive; добавить CSS states for disabled/pending/stale/offline, focus rings and reduced motion.
6. Обновить screenshot UI hiding list: решить, попадает ли status bar/фреймы на снимок; существующая `setMapChromeVisible()` должна скрывать все новый overlay/status elements и восстанавливать исходную видимость даже при ошибке/cancel.

**Выход:** новая визуальная структура на mock state; поиск/карта пока могут использовать адаптеры текущего state.

### Этап 5 — Единый поток точки и синхронизация

1. Добавить единственные функции `setSearchDraft`, `validateDraft`, `commitPoint`, `restorePoint`, `renderActivePoint`, `renderRoadResult`, `syncMapMarker`.
2. Перевести все source flows (map click/dblclick, photo, history, bookmark, address, coords, roads, geolocation, MSK) на этот контракт. Source обязателен; preview не пишет history.
3. Поставить SuggestView один раз на новые input elements. Для локальных подсказок — settlement/street/houses hierarchical endpoint; online selection -> кандидат. Показать provider/source/precision и request error.
4. Реализовать checkbox guard: armed => не принимать обычный map point event; disable km apply/slider commits, сохраняя pan/zoom. Button commit — единственная явная фиксация черновика.
5. Для coordinate mode — parse/validate WGS/DMS; convert on blur/change with debounce, stale guard; CRS cycling 1/2/3/WGS and clear uncertainty note. Не угадывать zone автоматически без official overlay.
6. Для road mode — search suggestion sets road draft/state; slider/number updates preview + coordinate fields; confirm commits once. Ordinary `locate()` should not silently change mode or clear source text.
7. Для map/slider synchronization: slider `input` preview; release/change commits once. Committing new map point optionally set nearest km for current road; when no selected road, nearest route result should be shown without falsely asserting official route chainage.
8. Add region warning to all manual coordinate sources; allow explicit cancel/edit, no silent clamp.
9. Add race guards to all async lookups (`locate`, MSK conversion, reverse address) and only latest active point updates result/history.

**Выход:** all single-point paths tested through same state transaction; existing endpoints kept.

### Этап 6 — Правая карточка, действия и статус-bar

1. Render empty state on boot and populate current point details only from active point; no old result accidentally survives `clear/reset`.
2. Add field-level copy controls with accessible announcements. WGS copy always available; MSK/address/district disabled unless verified/available.
3. Add star save action; update bookmark model/API, ensure source-specific name and metadata persistence.
4. Unify `copyRoadInfo()`/new top copy/status bar with a formatter; preserve current text format via regression fixture. Copy should not throw if Clipboard API unavailable; fallback remains.
5. Connect screenshot action to current screenshot flow; adapt naming based on priority road+km/address/coordinates and ensure input overlays are hidden/restored.
6. Add statusbar target-to-region, help modal, auto screenshot toggle/delay, settings and confirmed workspace reset. Remove original Rostov/help controls after all paths are migrated.
7. Ensure current status text updates after map point, address selection, km slider, favorite restore and batch active point.

**Выход:** no duplicate source of displayed/copied/screenshot identity.

### Этап 7 — Batch search experience and editing

1. Change batch overlay into tab UI, retain `/api/batch/upload`, file limits, invalid file errors and EXIF result.
2. Add stable point IDs, editable name, coordinate display, select, per-row edit/delete, clear all; `textContent` only for uploaded filenames/names to avoid XSS.
3. Implement markers as a layer keyed by point ID; no lat/lon fuzzy lookup (current `selectBatchPoint` finds marker by coordinate tolerance, duplicate coords are ambiguous).
4. Implement one active marker edit state with green/selected style, drag/confirm/cancel; while editing, disable ordinary map commits and non-active marker interactions. Keep Leaflet panning usable if drag marker handles are clear.
5. Show-all toggle uses bounds fitting once; list selection with show-all off places/focuses only selected marker. Selection updates right frame/status without losing list scroll/tab state.
6. Save batch list/selected ID/edit state per user session; when reloaded, restore safely and recompute markers, never restore stale upload bytes.
7. Batch result lookup should be explicit per selected row; throttle/sequence guard and use the same formatter/right-side result pipeline; don't run many simultaneous API locate requests.

**Выход:** functional batch workflow, event handling isolated from single-point map mode.

### Этап 8 — Settings auto-save and user session restore

1. Rehome radius/exclude filters, source/map selectors, boundary options, screenshot settings, account/users, theme/defaults into modal sections.
2. Persist per-user preferences with debounced updates; server response is canonical. Keep account/user create/password mutations explicit buttons.
3. Implement workspace autosave after UI transitions, map `moveend/zoomend`, tab/mode selection and active-point commit. Debounce map events; snapshot only serializable state.
4. Restore in order: authenticate -> fetch preferences/workspace/bookmarks -> apply theme/source availability -> initialize layers -> restore map view/layout/tab/draft/active point/batch markers -> only then optionally fetch current road result. Avoid saving default placeholder before server response arrives.
5. Offline/network failures: keep last local copy as temporary fallback, display unsaved indicator, retry with backoff; do not overwrite newer server state after conflict.
6. Implement reset workspace with modal confirmation; split from preferences/bookmark purge. Ensure return-to-default action persists and behaves identically on next login.

**Выход:** session state resumes after logout/browser restart/login and remains per-user.

### Этап 9 — Regression, accessibility, release

1. Run `pytest` including backend/auth/new persistence/per-user tests; package tests `packages/offline_geo/tests` separately according to project commands; inspect lint/static script syntax if configured.
2. Add API tests: login/auth, unrelated user isolation for bookmarks/preferences/workspace, malformed/oversize payload, concurrent/revision behavior, legacy migration idempotence, reset semantics, boundary/filter, km snap, route section gaps.
3. Add browser/manual or automated E2E matrix at desktop 1920×1080, 1366×768, 1280×720, narrow 640 px; light/dark; keyboard only. Existing project has no identified frontend test harness, so identify whether Playwright is allowed before adding a dependency; otherwise provide repeatable manual QA checklist.
4. Verify all click paths: map point (existing double-click or agreed single-click), Search armed blocks map commit and km; disarm restores, place confirms, slider-to-fields sync, fields-to-map sync, clear action, settings autosave, mode/tab switch not clearing state, restore after refresh.
5. Verify all data sources: local/external roads, offline missing index/tile errors, address provider unavailable/throttle, invalid/out-of-region results, MSK profile/zone warning, screenshot save/fallback.
6. Verify batch: mixed valid/invalid images, 0/1/N markers, duplicate coordinates, edit one and check others unchanged, cancel drag, clearing list, list selected state and restore.
7. Visual acceptance: no page-wide accidental scroll; controls fit visible panel; right-frame empty state; map available; no overlap on statusbar; accessibility names/focus; Leaflet attribution visible.
8. Update `README.md`/`TECHNICAL_DESCRIPTION.md` only after implementation; add migration/backup/recovery instructions; review diff, branch, commit/PR only when asked.

## 5. Acceptance criteria (definition of done)

- Existing GitHub version and current working changes remain recoverable; UI work lives only on a new isolated feature branch/worktree; no force push or destructive checkout.
- All three left tabs work and keep their state on tab switching; legacy search tab is replaced with batch search; no hidden duplicate toolbar or duplicate IDs.
- Search text never commits a map point without explicit confirmation. Armed search blocks normal map point selection and official km movement; selection/confirmation transitions to synchronized committed point as specified.
- WGS decimal/DMS and MSK conversion are mathematically correct within conversion library tolerance; manual outside-region coordinate produces warning/rejection; no unverified automatic zone/district claim.
- Road selection, official km slider, map marker, current coordinate fields and status bar represent the same coordinates/chainage; nearest km uses projection to segments and is tested with sparse geometry/gaps.
- Right pane empty until an actual point/result exists; all copy buttons copy the value/format indicated; save to favorite is per-user; screenshot still works.
- Settings auto-save to authenticated per-user state, restore after new session; account changes and roles still work; Reset actions clearly scoped.
- Batch uploads multiple valid files; names editable, rows deletable, map display toggle works; one selected draggable point can be edited/confirmed/cancelled without moving others or creating regular map points.
- Existing tests remain green, new tests cover migration/permissions/state/event edge cases; all known limitations disclosed in UI/docs.

## 6. Decisions to confirm before implementation

1. **Click semantics:** keep current double-click for normal map-created points, or make single-click the normal select behavior? Recommendation: do not change global map gesture; retain explicit existing dblclick or add a separate point-pick tool. Search-armed and batch edit must intercept their own map interactions.
2. **“Автоматическое сохранение всех настроек”:** confirm per-user prefs/workspace in SQLite; map state autosaves too; account/password/user writes remain explicit.
3. **Legacy bookmark ownership:** move existing shared bookmarks to current admin user or preserve a read-only shared list during migration? Recommend admin import + JSON backup.
4. **Reverse geocoding/address and municipality:** choose provider/data source, API key/licensing/privacy and acceptable precision. Until implemented, show “не определено”, not empty-looking fake data.
5. **Auto CRS zone:** can only be reliable with official zone/cadastral-area polygon data; should we ship manual cycle now and add authoritative auto lookup later? Recommendation: yes.
6. **Batch point placement:** approve draggable selected marker with save/cancel, or insist on click-to-move state machine? Recommendation: drag with explicit edit mode.
7. **Reset scope:** clear only workspace/history, or also preferences and favorites? Recommendation: workspace reset separate from destructive full reset.
8. **Branch strategy:** separate feature branch in existing GitHub repo, or a separate GitHub fork repository? Recommendation: feature branch/PR in current repo, based on an agreed clean checkpoint, while preserving original branch and dirty work.

## 7. Git / delivery workflow guardrails

- Do not start by editing `static/index.html` on the current dirty worktree. Record current HEAD/status; preserve all pre-existing changes and generated design assets.
- Do not commit, push, create GitHub fork, or modify remote branch without explicit user instruction. When authorized: branch `feature/ui-variant-a`; normal commit(s); PR into agreed target branch; include DB migration and recovery steps.
- Do not use force push, `git reset --hard`, `git clean`, or discard current modified files.
- Keep changes staged by phase and run tests before each milestone commit if user later requests implementation.
- At delivery give changed paths, migrations/backups, tests actually run and actual result (not assumed), open provider/UX decisions, and screenshot evidence.
