# Unity Tools Guide

Два read-only инструмента для AI-агента для навигации по Unity-проекту:

- **`unity_find.py`** — **asset graph уровень**: кто на что ссылается через guid, навигация по файлам, поиск спрайтов, audit orphan'ов и missing ссылок. Работает по всему проекту.
- **`unity_read.py`** — **content уровень**: что лежит *внутри* одного Unity YAML файла. Иерархия GameObject'ов в сценах/префабах, поля компонентов, дамп ScriptableObject'ов и Material'ов — всё с `L<N>:` якорями для Edit tool.

**Философия:**
- Stateless, без индекса. `ripgrep` + regex по YAML/meta.
- Вывод всегда резолвлен: имена классов, имена ассетов, sub-sprite'ы — AI-агенту не нужно дозагружать файлы.
- Никакой записи в файлы. Тулы дают якоря (`L<N>:` + готовая YAML-строка) — редактирует агент через Edit tool.

**Запуск:** из корня проекта, `tools/unity_find.py <command>` или `tools/unity_read.py <command>`.
**Зависимости:** `ripgrep` в PATH, Python 3.10+. Никаких pip-пакетов.

---

# `unity_find.py`

Stateless CLI для навигации по Unity-ассетам без индекса. Использует `ripgrep` + regex по YAML/meta, резолвит guid → путь → тип → имя. Вывод сразу читабельный и сгруппированный.

---

## Когда использовать / когда нет

**Использовать для:**
- Поиск ссылок: кто на что ссылается, что от чего зависит
- Навигация по guid (в логах, в сериализации, в сообщениях об ошибках)
- Триаж сцен/префабов: что за компоненты на них висят
- Audit контента: неиспользуемые ассеты, битые ссылки
- Навигация по спрайтам внутри текстур (Sprite Mode = Multiple)

**НЕ использовать для:**
- Поиск по **значениям полей** внутри ассетов (`weight > 5`, `title contains ...`) — тут нужен Grep/ast-grep или прямое чтение
- Структурные правки полей в YAML (используй Edit tool с `-L` флагом для якорей)
- Анализ GameObject-иерархии сцен/префабов (parent/child) — требует полноценного YAML-парса, не делаем
- Визуальные характеристики спрайтов (pivot, размеры, color) — живут в TextureImporter, регексом не читаем

---

## Команды

### `guid <path>` — guid ассета по пути

```bash
tools/unity_find.py guid "Assets/Resources/Content/Things/Candies/Candy Chocolate.asset"
# → 48186f4ad235d42fc91d37f1250d6a4d
```

Читает `.meta` файл, возвращает 32-символьный hex guid. Используй когда нужно получить guid для дальнейшего поиска или когда видишь путь в логе и хочешь узнать его идентификатор.

---

### `path <guid>` — путь ассета по guid

```bash
tools/unity_find.py path 48186f4ad235d42fc91d37f1250d6a4d
# → Assets/Resources/Content/Things/Candies/Candy Chocolate.asset
```

Инверсия `guid`. Используй когда встречаешь незнакомый guid в сериализованных данных, в сообщениях об ошибках ("missing script reference: guid XXX"), в коммитах и т.п.

Выход с ошибкой если guid не найден (это нормально — возможно ассет удалён, см. `missing`).

---

### `refs <path|guid>` — кто ссылается на ассет

```bash
tools/unity_find.py refs "Assets/Resources/Content/Tags/Colors/Color-Red.asset"
```

**Вывод сгруппирован по типу ссылающегося файла** и резолвит имена/классы:

```
TagSpec: Color-Red  [Assets/Resources/Content/Tags/Colors/Color-Red.asset]
guid: f66d56b94fa074ea6a1526c5e6195a74
referenced by 27 file(s):

[Scene]  (1)
  Game                                     Assets/Scenes/Game.unity

[OrderTemplateSpec]  (15)
  Aiko - Palette Chaos                     Assets/.../Aiko - Palette Chaos.asset  ×5
  Amelie - Candy Rainbow                   Assets/.../Amelie - Candy Rainbow.asset  ×4
  ...
```

- Можно передавать **или путь, или 32-символьный guid** — скрипт сам определит.
- Числа `×N` — количество ссылок в одном файле.
- Типы группировки: Scene / Prefab / Material / Animation / или имя класса для ScriptableObject (ThingSpec, TagSpec, OrderTemplateSpec и т.д.).
- **Для текстур с Sprite Mode = Multiple** автоматически добавляется breakdown по sub-sprite'ам в скобках: `×103 (button-hovered×44, button×22, ...)`.

**Флаги:**
- `-L` / `--locations` — показать номера строк и сниппеты под каждым файлом (для редактирования через Edit tool, сниппет это готовый `old_string`).
- `--json` — машиночитаемый вывод.

---

### `deps <path>` — исходящие зависимости ассета

```bash
tools/unity_find.py deps "Assets/Resources/Content/Characters/Aiko.asset"
```

Показывает **что использует** указанный ассет. Группирует по типу зависимости, резолвит имена:

```
Aiko  [Assets/Resources/Content/Characters/Aiko.asset]
outgoing refs: 23 unique guid(s)

[OrderTemplateSpec]  (5)
  Aiko - Art Supplies    Assets/Resources/Content/OrderTemplates/Aiko/Aiko - Art Supplies.asset
  ...
[SealSpec]  (1)
  Aiko                   Assets/Resources/Content/Seals/Aiko.asset
[TagSpec]  (4)
  Color-Black            ...
[ThingSpec]  (12)
  Button Round Medium    ...
```

Одной командой получаешь полный профиль ассета — что в нём лежит. Заменяет цепочку из Read + поиск по guid'ам.

**Для текстур с Sprite Mode = Multiple** в строке ссылки показываются используемые sub-sprite'ы: `arts  Assets/Sprites/arts.png  ×7  (button, button-clicked, button-hovered, pane)`.

**Фильтры применяемые автоматически:**
- Self-references (ассет ссылается на свой guid) — не показываются
- NULL guid (`0000...0000`) — не показывается
- Unity built-in guid'ы (префикс `0000000000000000...`) — не показываются

**Флаг `-L` / `--locations`** — как в `refs`, добавляет номера строк и сниппеты.

---

### `instances <ClassName>` — все ассеты данного класса

```bash
tools/unity_find.py instances ThingSpec
# → все 25 ассетов с m_Script = ThingSpec
```

Находит `<ClassName>.cs.meta` → guid скрипта → все файлы с `m_Script: {..., guid: <guid>}`. Использует: "покажи все персонажи / все теги / все печати в проекте".

Если несколько `.cs.meta` с одним именем класса — пишет warning в stderr.

---

### `components <prefab>` — скрипты на префабе

```bash
tools/unity_find.py components "Assets/Resources/Prefabs/UI/Seal-UIView.prefab"
```

```
Prefab: Seal-UIView  [...]
m_Script entries: 12 (8 unique class(es))
  UIStyler               ×3
  L10NComponent          ×2
  L10NFontAdapter        ×2
  SealUIView             ×1
  ...
```

Парсит все `m_Script: {..., guid: ...}` в файле, резолвит guid → имя класса через `.cs.meta`. Быстрый ответ на "что делает этот префаб?" без открытия в Unity.

---

### `summary <scene>` — инвентаризация сцены

Alias для `components`, но семантически — "что есть в этой сцене": все классы скриптов с количеством инстансов. На `Game.unity` (тяжёлая основная сцена) выполняется за ~1.5с.

```bash
tools/unity_find.py summary "Assets/Scenes/Game.unity"
```

```
Scene: Game  [Assets/Scenes/Game.unity]
m_Script entries: 413 (57 unique class(es))
  UIStyler                    ×51
  L10NFontAdapter             ×30
  L10NComponent               ×14
  ThingContainerSpawner       ×4
  GameCycleController         ×1
  GameSessionController       ×1
  ...
```

Нужно для триажа: "какие системы вообще задействованы в этой сцене", "что отвечает за что", "где искать контроллер X".

---

### `orphans <folder>` — неиспользуемые ассеты

```bash
tools/unity_find.py orphans "Assets/Resources/Content/Tags"
```

Находит ассеты в папке, на которые нет статических ссылок в YAML файлах проекта. Кандидаты на удаление.

**Как работает:**
1. Собирает guid'ы всех ассетов в папке (игнорирует папки-сами-по-себе — у них тоже есть `.meta`).
2. Одним rg-вызовом ищет ссылки на эти guid'ы в проекте.
3. Делит кандидатов на **orphans** и **runtime-loaded** через эвристику `Resources.Load`.

**Эвристика Resources.Load (точная, не фолсит):**
- Сканирует `Assets/Scripts/**/*.cs` на паттерны `Resources.Load<T>("literal")`
- Учитывает **только полные строковые литералы** (без конкатенации `+ x` и без интерполяции `{x}`)
- НЕ учитывает `LoadAll` и интерполированные пути — они слишком широкие и неинформативные, regular reference scan их покрывает
- Если путь ассета (relative к `Assets/Resources/`) совпадает с одним из найденных литералов — ассет перемещается в блок "runtime-loaded"

**Флаги:**
- `--strict` — включает `GameContentSpec.asset` в набор ссылок. По дефолту (loose) он исключён, потому что это агрегатор контента и прячет реальные orphan'ы. В strict режиме видны только ассеты, которые не зарегистрированы НИГДЕ.
- `--no-resources-check` — отключает эвристику, все не-референсированные считаются orphan'ами.

**Два режима использования:**
- `loose` (default) — "найди кандидатов на удаление, даже если они зарегистрированы в GameContentSpec". Используй когда чистишь неиспользуемый контент.
- `strict` — "найди только действительно неиспользуемое". Используй для финальной проверки.

**Вывод:**
```
orphans in Assets/Resources/Content/Tags  [loose + Resources.Load filter]
scanned 58 asset(s), 8 orphan(s), 0 likely string-loaded:

[orphans]
  TagSpec   Group-Precious    Assets/Resources/Content/Tags/Groups/Group-Precious.asset
  TagSpec   Hole-1x           Assets/Resources/Content/Tags/Hole/Hole-1x.asset
  ...

[runtime-loaded — exact Resources.Load<T>("path") in Assets/Scripts/*.cs]
  GameContentSpec  GameContentSpec  Assets/Resources/Content/GameContentSpec.asset
```

**Внимание:** скрипт не видит ассеты, которые:
- Грузятся через `Resources.LoadAll<T>(folder)` — в этом случае loose mode покажет их как "orphans", но это могут быть ложные срабатывания. Используй `strict` чтобы сверить с реальным registry.
- Грузятся через Addressables — вообще не поддерживаются.
- Используются только в editor-коде (`Assets/Editor/`, `#if UNITY_EDITOR`) — скан идёт по всем .cs, должен ловить, но бывают edge cases.

---

### `missing [folder]` — битые ссылки

```bash
tools/unity_find.py missing Assets/Resources    # narrow scan
tools/unity_find.py missing                      # whole project
```

Находит ссылки на guid'ы, которых нет в проекте (broken references — то, что Unity показывает как "Missing (Script)" или розовые поля в Inspector).

**Алгоритм:**
1. Собирает все существующие guid'ы из всех `.meta` файлов.
2. Сканирует все не-meta файлы в указанной папке на **канонические ссылки** вида `{fileID: X, guid: Y, type: Z}`.
3. Для каждой проверяет: guid в наборе? Если нет — broken.

**Фильтры (автоматически):**
- Только канонические `{fileID, guid, type}` формы — внутренние guid'ы AudioMixer snapshot'ов (они выглядят как `- guid: X` вне этого формата) игнорируются.
- Self-references (prefab variant ссылается на свой guid) — не считаются битыми.
- Unity built-in guid'ы (префикс `0000000000000000...`) — не считаются битыми (это встроенные Unity ассеты — default sprites, lights, materials).

**Вывод сгруппирован по missing guid**, отсортирован по impact (сколько раз упоминается):

```
missing references in Assets/Resources: 18 unique guid(s), 51 occurrence(s)

guid: 306cc8c2b49d7114eaa3623786fc2126  (9 ref(s))
  Assets/Resources/Prefabs/Containers/BoxContainer.prefab:L634  m_Script: {fileID: 11500000, guid: 306cc8c2..., type: 3}
  Assets/Resources/Prefabs/UI/SealBook/SealBookEntry.prefab:L77  m_Script: ...
  ... +4 more
```

**Типичное применение:** прогнать после рефакторинга / удаления скриптов / слияния веток. Если `missing` на `Assets/Resources` что-то показывает — есть broken references которые Unity ещё не показал (или показал, но ты не заметил).

---

### `sprite <name>` — найти спрайт по имени (+ готовый YAML-ref)

```bash
tools/unity_find.py sprite Discord-Symbol-Blurple_0
```

Ищет sub-sprite по имени во всех текстурах проекта и сразу печатает **готовую к вставке YAML-ссылку** для Edit tool.

```
1 match(es) for 'Discord-Symbol-Blurple_0' [substring]
  Discord-Symbol-Blurple_0
    Assets/Sprites/special/Discord-Symbol-Blurple.png   [mode: Single]
    {fileID: 21300000, guid: fa614bb908c30491ea9947971ae334ec, type: 3}
```

**Важно — Single vs Multiple.** Unity хранит Single-mode спрайт под фиксированным `fileID: 21300000` (= classID(213) * 100000), а Multiple — под индивидуальным `internalID` из `nameFileIdTable`. Тул сам читает `spriteMode` из .meta и подставляет правильное число. Если просто взять `internalID` из meta у Single-текстуры и вставить его в сцену — ссылка будет **битой**, спрайт не отобразится. Этот тул защищает от такой ошибки.

**Флаги:**
- `--exact` — только точное совпадение имени.
- `--all` — выключить лимит 50 результатов.

**Use case:** назначить спрайт в сцене/префабе через Edit tool — скопируй блок `{fileID: ..., guid: ..., type: 3}` из вывода как `new_string`.

---

### `sprites <texture.png>` — sub-sprite'ы текстуры

```bash
tools/unity_find.py sprites "Assets/Sprites/arts.png"
```

```
Assets/Sprites/arts.png  [14 sub-sprite(s), mode: Multiple]
guid: 6bc24bf635378465cbc290d0c7d4a239
orphan sub-sprites: 1

  button-hovered                 fileID: 1425635759             × 46 ref(s)
  button                         fileID: -2065609677            × 24 ref(s)
  ...
  cursor                         fileID: 893120411              × 0 refs  ← orphan
```

Парсит `nameFileIdTable`, учитывает `spriteMode` (для Single выводит `21300000` вместо internalID), считает refs по текстуре. Отсортировано по impact, orphan'ы в конце. Для составления YAML-ссылки — комбинируй `fileID` из строки + `guid` из заголовка.

**Use case:** почистить неиспользуемые sub-sprite'ы внутри текстуры, которые нельзя найти обычным `orphans` (он работает на уровне всего файла, а sub-sprite'ы живут внутри).

**Флаги:**
- `--no-refs` — не считать refs (только список имён). Быстрее, если нужен просто список.

---

## Эвристики и ограничения

### Что скрипт НЕ видит
- **Resources.LoadAll** — прогнозируется только по exact Load, поэтому ассеты загружаемые через LoadAll могут попасть в orphan-кандидаты в loose режиме. Используй `--strict` для сверки.
- **Addressables** — вообще не поддерживаются.
- **Динамические ссылки через `AssetDatabase.LoadAssetAtPath`** в editor-коде — не парсится.
- **Binary формат сцен/префабов** — всё делается предположением, что проект в Force Text режиме (Edit > Project Settings > Editor > Asset Serialization Mode). У нас это так.

### Производительность
На проекте ~500 ассетов в Assets/Resources:
- `guid`, `path`, `instances`, `components`, `sprite` — <0.5с
- `refs`, `deps` — 0.5–2с
- `summary Game.unity` — ~1.5с
- `orphans Assets/Resources/Content` — ~2с
- `missing` project-wide — ~1с
- `sprites <texture>` — ~0.5с

Все быстро. Если что-то тормозит — issue.

### Философия
- **Statelessness**: никакого индекса, никакого кеша. Каждый запуск — чистый.
- **Fast path = ripgrep**: всё что можно свести к одному rg-вызову, сводим.
- **Resolved output**: агенту нужна готовая информация, а не сырой grep. Имена классов, имена ассетов, sub-sprite'ы — всё резолвится сразу.
- **Никакой структурной правки YAML** — для этого используй Edit tool с `-L` флагом (он даст готовые `old_string` якоря).

---

## Типичные рабочие сценарии

### 1. "Кто использует этот TagSpec — можно удалить?"
```bash
tools/unity_find.py refs "Assets/Resources/Content/Tags/Colors/Color-Purple.asset"
```

### 2. "Что внутри этого персонажа?"
```bash
tools/unity_find.py deps "Assets/Resources/Content/Characters/Aiko.asset"
```

### 3. "Какие неиспользуемые теги у нас в проекте?"
```bash
tools/unity_find.py orphans "Assets/Resources/Content/Tags"
# и для сверки:
tools/unity_find.py orphans --strict "Assets/Resources/Content/Tags"
```

### 4. "В сцене Game.unity что-то сломалось, какие там вообще системы?"
```bash
tools/unity_find.py summary "Assets/Scenes/Game.unity"
```

### 5. "Вижу в логе missing script с guid abc123..., что это было?"
```bash
tools/unity_find.py path abc123...
# или если уже удалено, посмотреть где ссылались:
tools/unity_find.py missing | grep abc123
```

### 6. "После рефакторинга — есть ли broken references?"
```bash
tools/unity_find.py missing Assets/Resources
tools/unity_find.py missing Assets/Scenes
```

### 7. "Хочу заменить все ссылки на старый спрайт button-old на button-new"
```bash
# 1. Найти текстуру со старым спрайтом
tools/unity_find.py sprite button-old
# 2. Найти кто использует эту текстуру именно через этот sub-sprite
tools/unity_find.py refs -L "Assets/Sprites/arts.png"
# 3. В refs -L будут номера строк со сниппетами — использовать Edit tool
```

### 8. "Какие sub-sprite'ы в arts.png реально используются?"
```bash
tools/unity_find.py sprites "Assets/Sprites/arts.png"
# orphan'ы показаны в конце списка
```

### 9. "Все ThingSpec'и в проекте"
```bash
tools/unity_find.py instances ThingSpec
```

### 10. "Что вообще делает этот префаб?"
```bash
tools/unity_find.py components "Assets/Resources/Prefabs/UI/Seal-UIView.prefab"
```

---

## Развитие скрипта

Что делать НЕ планируется (сознательно):
- Чтение/запись значений полей Spec'ов — это отдельная задача для `unityparser` pypi пакета, ad-hoc.
- Парсинг GameObject-иерархии — сложно, редко нужно.
- SpriteAtlas membership — отдельный .spriteatlas файл, нишевая фича.
- In-place remap guid'ов — опасно для AI, лучше Edit tool пофайлово.

Что может быть добавлено если возникнет реальная задача:
- Поддержка Addressables (распарсить `AddressableAssetGroup`)
- `between <A> <B>` — ссылается ли A на B напрямую (сейчас через `deps A | grep B`)
- Поиск по сигнатуре `TextureImporter` полей (размер, filter mode) — для audit'а импорт-настроек

Если встречаешь задачу, которую тул не покрывает — сначала спроси, имеет ли смысл расширять `unity_find.py` или писать ad-hoc скрипт. Не плодим функциональность впрок.

---

# `unity_read.py` — чтение сцен и префабов

Парный тул к `unity_find.py`. Пока `unity_find` работает на **asset-уровне** (кто на что ссылается, что где лежит), `unity_read` работает на **scene/prefab-уровне** — GameObject-иерархия, компоненты, значения полей *внутри* одного файла.

**Read-only.** Никогда не пишет в файлы. Даёт агенту точные координаты (`L<N>` + YAML-строка) для правки через Edit tool.

Работает на `.unity` и `.prefab` — формат GameObject/Transform идентичен.

## Команды

### `tree <file>` — иерархия GameObject'ов

```bash
tools/unity_read.py tree "Assets/Resources/Prefabs/UI/Seal-UIView.prefab"
```

```
Assets/Resources/Prefabs/UI/Seal-UIView.prefab  [12 GameObjects, 48 docs]
└─ Seal-UIView  (Canvas, CanvasRenderer, Image, SealUIView, UIStyler)  [12345]  L10
   ├─ Title  (CanvasRenderer, TextMeshProUGUI, L10NFontAdapter)  [67890]  L156
   └─ Frame  (CanvasRenderer, Image, UIStyler)  [11111]  L287
```

- `[<fileID>]` — идентификатор GameObject'а внутри файла (нужен для `inspect` / `path`)
- `L<N>` — строка где определено `m_Name:` этого GameObject'а (якорь для Edit tool)
- Компоненты в скобках inline

**Флаги:**
- `--root <name>` — показать только поддерево заданного GameObject. Для больших сцен обязателен — иначе дерево огромное.
- `--depth N` — ограничить глубину.
- `--filter <ComponentClass>` — показывать только ветви содержащие GameObject с этим компонентом. Полезно для "где в сцене UIStyler'ы".
- `--expand-components` — компоненты на отдельных строках вместо inline (для чтения структуры в деталях).

Для **больших сцен** (>300 GameObject'ов) без `--root` тул по дефолту показывает только root-уровень и выводит подсказку как углубиться. Защита от 10000-строчного вывода.

---

### `find <file> <name>` — поиск GameObject по имени

```bash
tools/unity_read.py find "Assets/Scenes/Game.unity" Button
```

```
12 match(es) for 'Button' in Assets/Scenes/Game.unity:
  [113876295]  GloablUICanvas/PauseMenu/MenuPanel/ResumeButton       L1784
  [938848401]  GloablUICanvas/PauseMenu/MenuPanel/ExitGameButton     L11433
  ...
```

Substring match. Возвращает fileID + полный hierarchy path + `L<N>` якорь.

Типичные use-case'ы:
- "Куда в сцене спрятан GameObject с именем X" — один запрос, путь готов
- "Сколько ResumeButton'ов в сцене?" — видно по количеству матчей

---

### `inspect <file> <fileID|name>` — Inspector view

```bash
tools/unity_read.py inspect "Assets/Scenes/Game.unity" ResumeButton
```

```
GameObject: ResumeButton  [fileID: 113876295]
Path: GloablUICanvas/PauseMenu/MenuPanel/ResumeButton
Name anchor: Assets/Scenes/Game.unity:L1784
Components: 6

  RectTransform  [fileID: 113876296]  L1790
    L1797:    m_LocalRotation: {x: -0, y: -0, z: -0, w: 1}
    L1798:    m_LocalPosition: {x: 0, y: 0, z: 0}
    L1807:    m_AnchoredPosition: {x: 0, y: 0}
    L1808:    m_SizeDelta: {x: 0, y: 100}

  Image  [fileID: 113876299]  L1874
    L1887:    m_Color: {r: 1, g: 1, b: 1, a: 1}
    L1894:    m_Sprite: {fileID: -2065609677, guid: 6bc24bf..., type: 3}  → button (Assets/Sprites/arts.png)
    ...

  Button  [fileID: 113876298]  L1830
    L1859:      m_HighlightedSprite: {fileID: 1425635759, guid: ..., type: 3}  → button-hovered (Assets/Sprites/arts.png)
    L1860:      m_PressedSprite: {fileID: 275403282, guid: ..., type: 3}  → button-clicked (Assets/Sprites/arts.png)
    ...
```

**Ключевое:** каждая строка поля имеет:
- `L<N>:` — номер строки в файле
- Полный YAML-текст строки (буквально то, что написано в файле)
- `→ <resolved>` — резолв ссылок: asset-пути, sub-sprite имена, локальные GameObject'ы

**Для редактирования:** агент копирует строку после `L<N>:` как `old_string` в Edit tool, заменяет нужное значение, применяет. Тул ничего не пишет, агент полностью контролирует правку.

**Принимает:**
- Numeric fileID: `inspect <file> 113876295`
- Substring имени: `inspect <file> ResumeButton`
- Если матчей несколько — выбирается первый, warning в stderr.

**Флаги:**
- `--fields` — показать ВСЕ поля, включая `m_ObjectHideFlags`, `m_Enabled`, `m_Script` и прочие служебные. По дефолту они скрыты чтобы не засорять вывод.

**Что скрыто по дефолту:**
`m_ObjectHideFlags`, `m_CorrespondingSourceObject`, `m_PrefabInstance`, `m_PrefabAsset`, `m_GameObject`, `m_Enabled`, `m_Script`, `m_EditorHideFlags`, `m_RootOrder`, `m_Father`, `m_Children`, `serializedVersion`, и ещё несколько Unity-внутренних.

---

### `path <file> <fileID>` — обратный маппинг

```bash
tools/unity_read.py path "Assets/Scenes/Game.unity" 113876298
# → GloablUICanvas/PauseMenu/MenuPanel/ResumeButton  (Button on 'ResumeButton')
```

fileID → hierarchy path. Работает:
- на GameObject'ах (просто путь)
- на Transform'ах (путь владельца + "Transform of")
- на компонентах (путь + имя класса компонента)

Use-case: видишь в логе `GameObject[fileID=113876298]` или error в сцене со ссылкой на fileID — одной командой узнаёшь где это в иерархии.

---

### `show <file>` — dump содержимого SO / Material / single-doc asset

```bash
tools/unity_read.py show "Assets/Resources/Content/Things/Candies/Candy Chocolate.asset"
```

```
Assets/Resources/Content/Things/Candies/Candy Chocolate.asset  [1 document(s)]

=== ThingSpec: Candy Chocolate  [fileID: 11400000]  L3 ===
  L13:  m_Name: Candy Chocolate
  L16:  title: items.things.candy_chocolate
  L17:  size: 2
  L18:  hardness: 1
  L19:  tags:
  L20:  - {fileID: 11400000, guid: ..., type: 2}  → Assets/Resources/Content/Tags/Kinds/Food.asset
  L21:  - {fileID: 11400000, guid: ..., type: 2}  → Assets/Resources/Content/Tags/Shape/Shape-Uncommon.asset
  L22:  - {fileID: 11400000, guid: ..., type: 2}  → Assets/Resources/Content/Tags/Material/Material-Organic.asset
  L23:  - {fileID: 11400000, guid: ..., type: 2}  → Assets/Resources/Content/Tags/Size/Size-Large.asset
  L24:  prefab: {fileID: ..., guid: ..., type: 3}  → Assets/Resources/Prefabs/Things/SimpleThing.prefab
  L25:  skins:
  L26:  - sprite: {fileID: 21300000, guid: ..., type: 3}  → Assets/Sprites/items/candys/candy_chocolate_01.png
  L27:    tags:
  L28:    - {fileID: 11400000, guid: ..., type: 2}  → Assets/Resources/Content/Tags/Colors/Color-Green.asset
  ...
  L42:  weight: 12
  L43:  height: 0.3
```

Универсальный dump содержимого **не-GameObject YAML ассета**. Работает на:
- **ScriptableObject'ах** (`.asset`) — ThingSpec, TagSpec, CharacterSpec, OrderTemplateSpec, UIStyleSpec и т.п.
- **Materials** (`.mat`) — с разрешением wrapped-ссылок на текстуры и шейдер.
- **Multi-document .asset** — если у SO есть sub-assets (parent + child), показываются все документы.
- **Любом single-doc YAML** с классом ≠ GameObject/Transform.

**Ключевое:** каждая строка (включая list items) выдаётся с `L<N>:` якорем и резолвом ссылок. Это позволяет агенту **редактировать любое поле** через Edit tool, скопировав строку как `old_string` и заменив значение.

**Особенности:**
- **List items** (`- {fileID: ...}`) тоже показываются с резолвом — для массивов tags/skins/things видно всё содержимое.
- **Многострочные ссылки** (Unity обёртывает длинные `{fileID, guid, type}` на 2 строки) — lookahead'ом тул дотягивается и резолвит.
- **Nested структуры** (вложенные блоки в SavedProperties материалов, skin-массивы в SO) — preserved как в файле с соответствующим indent'ом.
- **Hidden fields** по дефолту скрыты: `m_ObjectHideFlags`, `m_PrefabInstance`, `m_Script`, `m_EditorHideFlags`, `serializedVersion` и т.п. Флаг `--fields` показывает всё.

**Защиты:**
- Если `show` вызван на файле с GameObject'ами (сцена/префаб) — тул выводит подсказку использовать `tree` или `inspect`. Флаг `--force` обходит.
- `inspect <SO.asset>` на SO без GameObject'ов автоматически redirects на `show` (с notice в stderr).

**Флаги:**
- `--fields` — показать все служебные поля.
- `--force` — дампить документы даже если файл содержит GameObject'ы.

**Use-case'ы:**
- "Какие у этого ThingSpec поля, что в тегах, какие скины?" → `show candy.asset`
- "Какой шейдер и текстуры у этого материала?" → `show foo.mat`
- "Хочу поменять `weight` / `title` / любое поле SO" → `show` → копировать `L<N>:` строку → Edit tool
- "Что внутри этого CharacterSpec — какие Things, какие TagFilters, какие OrderTemplates?" → `show` (всё с резолвом)

---

## Что тул НЕ делает

**По дизайну (скрипт сам):**
- **Не пишет в файлы.** Редактирование делает агент через Edit tool, используя `L<N>:` строки из вывода как `old_string`. Это полная прозрачность: агент видит что меняется, diff идёт через git нормально.
- **Не ищет структурное дерево PrefabInstance override'ов.** `PrefabInstance` документ показывается как отдельный документ, но nested-prefab иерархия не разворачивается из целевого префаба.

**Риски текстовой правки (знать перед Edit tool):**
- **Простые скалярные поля** (позиции, цвета, текст, числа, bool'ы, enum'ы) — безопасно, каждая такая правка это одна строка, одна замена.
- **Asset-ссылки** (поменять `m_Sprite` / `m_Material` на другой ассет) — безопасно, одна строка `{fileID: ..., guid: ..., type: ...}`. Новый fileID/guid бери из `unity_find path` или `unity_find sprite`.
- **Добавление/удаление элементов в list-полях** (`tags:`, `skins:`, `m_Component`) — возможно, но надо держать согласованность: YAML отступы, запятые, отсутствие висячих ссылок. Один добавленный item в `tags:` — нормально; добавленный компонент — уже сложнее, потому что нужно ещё писать сам документ компонента и обновлять список в родителе.
- **Добавление/удаление GameObject'ов и компонентов** — технически возможно через Edit, но хрупко: нужны уникальные fileID, обновление `m_Component` в GameObject'е, `m_Father`/`m_Children` в Transform'ах, корректные документы. Малейший рассинхрон → Unity не загружает файл. Для таких правок обычно дешевле сделать через Unity Editor и потом git-commit, чем править текст вручную.
- **Prefab overrides** (`m_Modifications` в `PrefabInstance`) — имеют свой хитрый формат с path-based записями. Редактировать вручную можно, но требует понимания формата.
- **Nested prefab variants** — `stripped` documents имеют минимальный вид, правки почти всегда правильнее делать в исходном префабе.

**Правило пальца:** если правка это **замена значения в одной строке** — смело через Edit tool с `L<N>:` якорем. Если это **изменение структуры** (добавить/удалить документы, переместить иерархию) — подумай дважды, часто проще открыть в Unity Editor.

## Разделение ответственности

| Задача | Инструмент |
|---|---|
| "Кто ссылается на этот ассет?" | `unity_find refs` |
| "Что этот Spec использует как ссылки?" (граф) | `unity_find deps` |
| "Какие компоненты на этом префабе в сумме?" (flat count) | `unity_find components` |
| "Все Spec'и класса X в проекте" | `unity_find instances` |
| "Неиспользуемые ассеты / битые ссылки" | `unity_find orphans` / `missing` |
| "Какая иерархия GameObject'ов в сцене/префабе?" | `unity_read tree` |
| "Какие компоненты у GameObject'а X и значения полей?" | `unity_read inspect` |
| "Найти GameObject по имени в сцене" | `unity_read find` |
| "Что это за fileID?" | `unity_read path` |
| "Показать все поля SO / Material'а с резолвом" | `unity_read show` |
| "Изменить скалярное поле (позицию, цвет, текст, sprite, weight)" | `unity_read inspect`/`show` → Edit tool с `L<N>` якорем |
| "Добавить/удалить GameObject или компонент" | Edit tool (возможно, но хрупко) или Unity Editor |
| "Работа с prefab variants / apply overrides" | Unity Editor (надёжнее) или Edit tool с пониманием формата |
| "Создать новый SO" | Скопировать существующий `.asset` + Edit (меняем `m_Name`, guid в `.meta`, значения полей) |

## Производительность

- `tree` на малом префабе: <50ms
- `tree --root X` на `Game.unity`: ~0.5с (доминирует resolve MonoBehaviour через PackageCache)
- `find`: ~50ms (только парс)
- `inspect`: ~200ms-1с (зависит от количества компонентов — каждый требует resolve script guid)
- `path`: ~50ms

Резолв `m_Script` guid → имя класса кешируется в пределах одного вызова, так что много компонентов одного типа (например, 50 × UIStyler) не бьёт по производительности.

## Типичные сценарии

### 1. "Что за префаб и что на нём?"
```bash
tools/unity_read.py tree Assets/Resources/Prefabs/UI/Seal-UIView.prefab
```

### 2. "Хочу поменять цвет кнопки"
```bash
tools/unity_read.py inspect Assets/Scenes/Game.unity ResumeButton
# найти строку L1887 с m_Color, скопировать в Edit tool, заменить значения
```

### 3. "Где в Game.unity все UIStyler'ы"
```bash
tools/unity_read.py tree Assets/Scenes/Game.unity --filter UIStyler --depth 5
```

### 4. "В логе ошибка про fileID 12345, что это"
```bash
tools/unity_read.py path Assets/Scenes/Game.unity 12345
```

### 5. "Какие кнопки есть в PauseMenu"
```bash
tools/unity_read.py tree Assets/Scenes/Game.unity --root PauseMenu
```

### 6. "Поменять spriteshowcased у Image в префабе"
```bash
tools/unity_read.py inspect Assets/Resources/Prefabs/UI/Seal-UIView.prefab Frame
# найти L строку с m_Sprite, resolved к sub-sprite имени
# через Edit tool заменить {fileID: X, guid: Y, type: 3} на новый
# (новый fileID можно получить через unity_find.py sprite <new-name>)
```

