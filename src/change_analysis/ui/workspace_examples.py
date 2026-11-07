"""Готовые примеры для рабочей области: проверки и процедуры, которые сразу работают.

Каждый пример — самостоятельный файл по контракту. Они не «иллюстрации в комментариях»:
эти тексты проверены запуском на демонстрационном прогоне (2002 пары, 502 различия,
385 смен решения) и дают осмысленные числа. Кнопка в интерфейсе подставляет пример в
редактор, дальше его можно менять и сохранять как свой.

Контракт проверки: ``CHECK = {id, label, params, entrypoint}``, ``run(mart, params)``
возвращает список строк с полями LABEL, SOURCE, ARGUMENT, CNT, TOTAL, METRIC,
APP_IDS, RESULT, CONFIDENCE.

Контракт процедуры-ридера: ``read_document(path) -> dict`` — приводит файл любого
формата к словарю, дальше работает универсальное ядро.
"""

from __future__ import annotations

from typing import Any

EXAMPLES: list[dict[str, Any]] = [
    {
        "key": "check_stop_factor_appeared",
        "title": "Проверка: сработал новый стоп-фактор",
        "stage": "check",
        "about": "Ищет правила, которые в MASTER не срабатывали, а в BETA отсекают заявку.",
        "params": {"app_ids_limit": 10, "low_confidence_cnt": 5},
        "code": '''# -*- coding: utf-8 -*-
"""Какие стоп-факторы начали срабатывать после выката."""

DEFAULTS = {"app_ids_limit": 10, "low_confidence_cnt": 5}
RULE_SUFFIX = ".rules.hit"


def run(mart, params=None):
    p = {**DEFAULTS, **(params or {})}
    total = mart.total_apps()
    hits, rules = set(), set()
    for row in mart.rows(path_prefix="decisionResult.product_trials", diff_flag=1):
        if not row["PATH"].endswith(RULE_SUFFIX):
            continue
        before = (row.get("VALUE_BEFORE") or "").lower()
        after = (row.get("VALUE_AFTER") or "").lower()
        if after == "true" and before != "true":
            hits.add(row["APP_ID"])
            rules.add(row.get("KEY") or row["PATH"])
    return [{
        "LABEL": CHECK["label"],
        "SOURCE": "AFTER",
        "ARGUMENT": ", ".join(sorted(rules)[:5]),
        "CNT": len(hits),
        "TOTAL": total,
        "METRIC": round(len(hits) / total, 4) if total else 0.0,
        "APP_IDS": sorted(hits)[:p["app_ids_limit"]],
        "RESULT": "FAIL" if hits else "PASS",
        "CONFIDENCE": "LOW" if 0 < len(hits) < p["low_confidence_cnt"] else "HIGH",
    }]


CHECK = {"id": "stop_factor_appeared",
         "label": "Сработал новый стоп-фактор",
         "params": DEFAULTS,
         "entrypoint": run}
''',
    },
    {
        "key": "check_logic_flip",
        "title": "Проверка: решение изменилось при тех же входных данных",
        "stage": "check",
        "about": ("Отделяет изменения логики от изменений входных данных: заявки, где решение "
                  "перевернулось, а внешние скоры остались прежними."),
        "params": {"app_ids_limit": 10},
        "code": '''# -*- coding: utf-8 -*-
"""Переворот решения при неизменных внешних скорах — это изменение стратегии."""

DEFAULTS = {"app_ids_limit": 10}


def run(mart, params=None):
    p = {**DEFAULTS, **(params or {})}
    total = mart.total_apps()
    flips, logic, data = set(), set(), set()
    for row in mart.segments():
        if not row.get("DECISION_CHANGED"):
            continue
        flips.add(row["APP_ID"])
        before = row.get("SCORE_OKB_BEFORE") or ""
        after = row.get("SCORE_OKB_AFTER") or ""
        if before == after:
            logic.add(row["APP_ID"])
        else:
            data.add(row["APP_ID"])
    share = round(len(logic) / len(flips), 4) if flips else 0.0
    return [{
        "LABEL": CHECK["label"],
        "SOURCE": "AFTER",
        "ARGUMENT": f"из {len(flips)} переворотов {len(data)} связаны с входными данными",
        "CNT": len(logic),
        "TOTAL": total,
        "METRIC": share,
        "APP_IDS": sorted(logic)[:p["app_ids_limit"]],
        "RESULT": "FAIL" if logic else "PASS",
        "CONFIDENCE": "HIGH" if len(logic) > 20 else "LOW",
    }]


CHECK = {"id": "logic_flip",
         "label": "Решение изменилось при тех же входных данных",
         "params": DEFAULTS,
         "entrypoint": run}
''',
    },
    {
        "key": "check_attribute_lost",
        "title": "Проверка: из стратегии пропали атрибуты",
        "stage": "check",
        "about": "Считает ATTRIBUTE_LOST и показывает самые «потерянные» ветки.",
        "params": {"app_ids_limit": 10, "top_prefixes": 5},
        "code": '''# -*- coding: utf-8 -*-
"""Что исчезло из ответа: состав стратегии изменился, а не значения."""

from collections import Counter

DEFAULTS = {"app_ids_limit": 10, "top_prefixes": 5}


def run(mart, params=None):
    p = {**DEFAULTS, **(params or {})}
    total = mart.total_apps()
    apps, prefixes = set(), Counter()
    for row in mart.rows(change_type="ATTRIBUTE_LOST"):
        apps.add(row["APP_ID"])
        prefixes[row.get("PREFIX") or row["PATH"]] += 1
    top = ", ".join(name for name, _ in prefixes.most_common(p["top_prefixes"]))
    return [{
        "LABEL": CHECK["label"],
        "SOURCE": "BEFORE",
        "ARGUMENT": top,
        "CNT": len(apps),
        "TOTAL": total,
        "METRIC": round(len(apps) / total, 4) if total else 0.0,
        "APP_IDS": sorted(apps)[:p["app_ids_limit"]],
        "RESULT": "FAIL" if apps else "PASS",
        "CONFIDENCE": "HIGH" if len(apps) > 50 else "LOW",
    }]


CHECK = {"id": "attributes_lost",
         "label": "Из стратегии пропали атрибуты",
         "params": DEFAULTS,
         "entrypoint": run}
''',
    },
    {
        "key": "reader_custom_format",
        "title": "Процедура-ридер: свой формат файла",
        "stage": "normalize",
        "about": ("Показывает, как подключить нестандартный формат: файл «ключ.путь = значение» "
                  "строчками превращается в словарь, и работает то же ядро сравнения."),
        "params": {},
        "code": '''# -*- coding: utf-8 -*-
"""Свой ридер: плоский текст «путь = значение» → словарь для ядра.

Формат файла (по строке на атрибут):

    decisionResult.decision = APPROVE
    decisionResult.product_trials[0].product_id = 15210

Квадратные скобки задают индекс массива, точки — вложенность. Значения
разбираются как JSON: числа, true/false, null, строки в кавычках.
"""

import json
from pathlib import Path


def _set(root, keys, value):
    node = root
    for depth, key in enumerate(keys):
        last = depth == len(keys) - 1
        if isinstance(key, int):
            while len(node) <= key:
                node.append(None)
            if last:
                node[key] = value
            else:
                if node[key] is None:
                    node[key] = {} if not isinstance(keys[depth + 1], int) else []
                node = node[key]
            continue
        if last:
            node[key] = value
        else:
            nxt = keys[depth + 1]
            node = node.setdefault(key, [] if isinstance(nxt, int) else {})


def _keys(path: str):
    out = []
    for part in path.split("."):
        name, _, rest = part.partition("[")
        out.append(name)
        while rest:
            index, _, rest = rest.partition("]")
            out.append(int(index))
            rest = rest.lstrip("[")
    return out


def _value(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text.strip()


def read_document(path: Path) -> dict:
    document: dict = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, raw = line.partition("=")
        _set(document, _keys(key.strip()), _value(raw))
    if not isinstance(document, dict):
        raise ValueError("корень документа должен быть объектом")
    return document
''',
    },
    {
        "key": "normalize_params",
        "title": "Процедура нормализации: свои правила",
        "stage": "normalize",
        "about": ("Не код, а параметры: что исключать и какие массивы сопоставлять по ключу, "
                  "а какие по индексу. Их же можно править на странице конфигурации."),
        "params": {
            "excluded_paths": ["appData.request_id", "appData.createDateTime",
                               "appData.sessionId"],
            "max_errors_reported": 20,
        },
        "code": '''# -*- coding: utf-8 -*-
"""Параметры процедуры нормализации: здесь показаны только те, что чаще меняют.

excluded_paths — технические поля, которые различаются у всех заявок и только
создают шум: время, идентификаторы запроса, номер сессии.

arrays — как сопоставлять массивы между срезами:
  mode="field"  + key_fields — элемент опознаётся по значению поля (product_id);
  mode="value"               — элемент опознаётся по собственному значению (списки);
  mode="index"               — порядок значим (векторы чисел).

buckets — необязательная группировка чисел в интервалы для отчёта. На признак
различия не влияет: он всегда считается по исходному значению.

max_errors_reported — сколько ошибок чтения показать, прежде чем свернуть список
(хотя бы одна ошибка показывается всегда).
"""

EXCLUDED_EXAMPLE = "appData.request_id"
''',
    },
]


def by_key(key: str) -> dict[str, Any] | None:
    for example in EXAMPLES:
        if example["key"] == key:
            return example
    return None
