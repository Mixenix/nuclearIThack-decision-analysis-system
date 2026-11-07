# Как работать с этим репозиторием

Документ для второго разработчика: как поднять, что где лежит, где точки расширения,
что нельзя ломать. Всё ниже проверено на этой машине; команды выполняются **из корня
репозитория** (он же корень приложения).

---

## 1. Быстрый старт (5 минут)

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install "nicegui==4.0.0" "psycopg[binary]==3.3.8" "psycopg-pool==3.3.3" \
    "jinja2==3.1.6" "markupsafe==3.0.4" "argon2-cffi==25.1.0" "httpx==0.28.1"
.venv/bin/pip install "pytest==9.2.0" "pytest-timeout==2.4.0" "playwright==1.56.0"

./scripts/pg.sh init      # локальный кластер PostgreSQL в .pgdata на порту 55432
./scripts/pg.sh start
cp .env.example .env      # и отредактировать пути: DATASET_ROOT, ARTIFACT_ROOT, PUBLIC_BASE_URL

PYTHONPATH=src .venv/bin/python -m change_analysis.cli bootstrap   # схема + пользователи + датасет
./scripts/web.sh          # http://127.0.0.1:8000  (analyst / analyst)
./scripts/worker.sh       # отдельный процесс; без него прогоны стоят в очереди
```

Полный прогон эталонного набора — либо кнопкой «Сохранить и запустить» на странице
`/configs`, либо из консоли:

```bash
PYTHONPATH=src .venv/bin/python -m change_analysis.cli run analyst
```

Ожидаемый результат на выданном наборе: **2 002 пары → 289 533 строки витрины,
**502 заявки с различиями, 385 со сменой решения**.

---

## 2. Карта репозитория

```
main.py                     точка входа веб-приложения (NiceGUI)
migrations/*.sql            нумерованные миграции; схема ca
scripts/                    pg.sh (кластер), web.sh, worker.sh
src/change_analysis/
  config.py                 единственное место, читающее окружение/.env
  contracts/types.py        FilterSpec, перечисления, общие типы
  pipeline/core.py          ЯДРО разбора: обход документа, сериализация значений,
                            режимы массивов. НЕ знает формата файла и предметной области
  pipeline/segments.py      слой разрезов (одна строка на заявку) — контракт витрины
  procedures/spr_*.py       ПОЛЬЗОВАТЕЛЬСКИЕ процедуры: нормализация, сравнение, агрегация
  checks/loader.py          загрузка и валидация пользовательских проверок (контракт CHECK)
  db/{connection,mart,migrations}.py   psycopg, MartAPI, накат миграций
  services/                 единственный API для UI (owner-проверки здесь, не в UI)
  worker/                   очередь (FOR UPDATE SKIP LOCKED), runner, heartbeat, прогресс
  ui/pages_*.py             страницы NiceGUI
  reports/{summary,export}.py   сводка и HTML-отчёт
  llm/adapter.py            независимое мнение ИИ (DeepSeek), ограниченный payload
  templates/report.html.j2  автономный HTML (inline CSS, без CDN)
tests/{unit,integration,e2e} автотесты (345 тестов)
tools/                      bench_report.py, acceptance_ui.py, validate_against_reference.py
docs/                       CONTRACTS, STATUS, BENCHMARKS, DEMO, DECISIONS, ARCHITECTURE_NOTE
```

Поток данных: `датасет → normalize → compare → aggregate → pivot_facts/membership →
сводка, pivot, drill-down, отчёт проверок, HTML`.

---

## 3. Тесты

```bash
# быстрые, без БД
PYTHONPATH=src .venv/bin/python -m pytest tests/unit -q

# всё, включая интеграционные и e2e (нужна ОТДЕЛЬНАЯ база!)
TEST_DATABASE_URL=postgresql://ca:ca@127.0.0.1:55432/change_analysis_test \
  PYTHONPATH=src .venv/bin/python -m pytest tests -q
```

Правила, которые уже встроены в `tests/conftest.py` и которые нельзя обходить:

* **Тесты никогда не пишут в `change_analysis`** — только в `change_analysis_test`;
  teardown отказывается удалять, если DSN не тестовая база.
* Один прогон полного e2e = реальный прогон на 2002 заявки (~2 минуты CPU).
* Есть `pytest-timeout` (240 с на тест): зависший тест падает с сообщением, а не
  вешает весь suite. Это появилось после того, как накопление прогонов в тестовой
  базе без `ANALYZE` положило планировщик в hash join и три запроса сводной висели
  12–40 минут. Если увидели такое — `VACUUM (ANALYZE)` по таблицам витрины.

Браузерные проверки (Playwright) живут отдельно от pytest:

```bash
.venv/bin/python -m playwright install chromium
APP_PORT=8095 PYTHONPATH=src .venv/bin/python main.py   # свой экземпляр
# дальше — любой скрипт на playwright, указывающий на 127.0.0.1:8095
```

---

## 4. Точки расширения

### 4.1. Новая проверка (самое простое)

```python
CHECK = {"id": "my_check", "label": "Что проверяем", "params": {...}, "entrypoint": run}

def run(mart, params=None) -> list[dict]:
    # mart.rows(**filters), mart.count(), mart.app_ids(), mart.segments(), mart.total_apps()
    return [{"LABEL": ..., "SOURCE": "BEFORE"|"AFTER", "ARGUMENT": ..., "CNT": ...,
             "TOTAL": ..., "METRIC": ..., "APP_IDS": [...], "RESULT": "PASS"|"FAIL",
             "CONFIDENCE": "HIGH"|"LOW"}]
```

Кладётся либо в рабочую область (`/workspace`), либо файлом в `SCRIPT_ROOT`.
Пять выданных проверок исполняются **без изменения файлов** — так и должно остаться.

### 4.2. Новая процедура этапа (normalize / compare / aggregate)

```python
PROCEDURE = {"id": ..., "label": ..., "stage": "normalize", "contract_version": 1,
             "params": {...}, "entrypoint": run}

def run(context, inputs, params): ...   # context: run_id, backend, log, progress, artifact_dir
```

Ядро (`pipeline/core.py`) не должно знать ни одного предметного имени поля: всё, что
зависит от формата ответа СПР, живёт в процедуре и её параметрах.

### 4.3. Новый входной формат

Формат задаётся процедурой нормализации, а не ядром: она получает пути к файлам и сама
решает, чем их читать. Уже поддержаны `.json`, `.jsonl`, `.ndjson`, `.xml`, `.yaml`,
`.yml`, `.toml`, `.ini`, `.cfg`, `.conf`, `.properties`, `.csv`, `.tsv`, а также
`.zip`, `.gz`, `.bz2`, `.xz` (снимаются прозрачно) — см. `procedures/spr_formats.py`.

Порядок для нового формата:

1. добавить расширение в `SUPPORTED_EXT` и читатель в `spr_formats.read_bytes`;
2. добавить маску в `services/datasets.py::SUPPORTED_MASKS`;
3. проверить, что документ совпадает с JSON: тест в
   `tests/unit/test_multi_format_readers.py` (образец — YAML/JSONL);
4. если формат не выражает массивы/`null` — дописать ограничение в `README.md` §4
   и в `docs/CONTRACTS.md`.

Ядро при этом не меняется: `pipeline/core.py` по-прежнему не знает ни одного
предметного имени поля.

---

## 4.4. Карта кода и контракты слоёв

Перед правкой незнакомой части проекта:

```bash
python tools/codegraph.py                 # слои, маршруты, циклы, отклонения от слоёв
python tools/codegraph.py --out /tmp/cg   # + codegraph.md / .json / .mmd
```

`tools/codegraph.py` строит граф импортов по `ast` и печатает читаемые таблицы: слои,
самые связанные модули, маршруты интерфейса, циклы (ленивые импорты не считаются),
отклонения от правил слоёв. Внешних зависимостей нет — только стандартная библиотека.

## 5. Инварианты, которые нельзя ломать

Проверяются автоматически или глазами ревьюера — нарушение ломает приёмку:

1. **502 / 385** на выданном наборе (и **248 / 186** на отложенном).
2. `CHANGE_TYPE` различает `SAME / VALUE_CHANGED / ATTRIBUTE_NEW / ATTRIBUTE_LOST`;
   `DIFF_FLAG` считается по значению, а не по бакету.
3. Технические поля (`appData.request_id`, `appData.createDateTime`) исключаются
   настройкой, а не константой в коде.
4. `mart.app_ids()` — уникальные заявки, `mart.count()` — строки; их нельзя путать,
   и `SUM(CNT)` по сводной не является числом заявок.
5. Пять выданных файлов проверок не редактируются.
6. Пользовательский код не исполняется в веб-процессе (загрузка идёт в отдельном
   интерпретаторе; список проверок читается `ast` без импорта).
7. У прогона две ревизии: `revision` (отчёт проверок) и `data_revision` (витрина).
   `CHECKS_ONLY` поднимает первую и не имеет права двигать вторую.
8. `SUCCEEDED` означает «отчёт открывается быстро»: worker прогревает чтение и
   обновляет статистику **до** публикации.
9. Чужие объекты недоступны ни по одному прямому URL: проверка владельца — в
   `services/`, а не в UI.

---

## 6. Стиль

* Код, имена и комментарии — по-английски; всё, что видит пользователь, — по-русски.
* Никаких `shell=True`, никакой конкатенации пользовательского текста в команды.
* Долгие операции — только через worker, в обработчике события их быть не должно.
* Новое числовое утверждение в документации — только с командой или запросом, которым
  его можно воспроизвести.
* Если что-то не проверено — так и написать («не проверено», и почему).

---

## 7. Чек-лист перед Pull Request

- [ ] `pytest tests/unit -q` зелёный; при правке БД/сервисов — полный `pytest tests`.
- [ ] Эталонный прогон по-прежнему даёт 502 / 385.
- [ ] Миграции идемпотентны и не удаляют данные.
- [ ] В документации обновлено то, что изменилось (STATUS / DECISIONS).
- [ ] Нет новых ключей, датасетов и материалов организаторов в коммите.
- [ ] Если менялось поведение — в `docs/DECISIONS.md` написано, почему так, а не иначе.

---

## 8. Куда смотреть, если что-то пошло не так

| Симптом | Что проверить |
|---|---|
| прогон висит в `QUEUED` | запущен ли `worker.sh`; таблица `ca.jobs` |
| сводная показывает 0 заявок | совпадает ли `data_revision` с ревизией `pivot_facts`; не остался ли в памяти старый код — перезапустить приложение |
| сводная стала медленной (секунды вместо долей) | мёртвые кортежи: `pg_stat_user_tables`, затем `VACUUM (ANALYZE)` |
| `Скачать HTML` отдаёт пустую таблицу проверок | не опубликована ли ревизия: отчёт показывает последнюю **опубликованную** |
| проверка в отчёте `ERROR` | это техническая ошибка исполнения, а не бизнес-`FAIL`; смотреть `ca.check_executions.error` |
| «Мнение ИИ не настроено» | `LLM_API_KEY_ENV` и сама переменная; без ключа блок честно молчит, числа не страдают |

Удачи. Если найдёте, что улучшить — начинайте с `docs/IMPROVEMENTS.md`: там уже
собран список с оценкой пользы и риска.
