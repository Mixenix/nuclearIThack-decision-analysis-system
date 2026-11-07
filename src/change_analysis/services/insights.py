"""Агрегация не по строкам, а по последствиям и по элементам стратегии.

Обычная сводная отвечает «сколько строк в каждой ячейке». Аналитику нужен другой
разрез: **какие изменения что за собой потянули** и **какие элементы стратегии
виноваты чаще других**. Здесь три независимых агрегации, все — из уже посчитанной
витрины, без пересчёта прогона.

1. :func:`archetypes` — «архетипы изменений»: каждая заявка получает метку по составу
   изменений (только входные данные / правила / модель / порог / офферы / комбинация),
   и мы считаем, сколько заявок и сколько **смен решения** приходится на каждый архетип.
2. :func:`element_impact` — «влияние элементов стратегии»: агрегация по правилу, модели,
   порогу и продукту, а не по пути атрибута. Считает заявки, смены решения и долю
   смен, не объяснимых входными данными.
3. :func:`cooccurrence` — «что меняется вместе»: пары групп изменений с коэффициентом
   Жаккара. Это **корреляция, а не причина** — так и подписано в интерфейсе.

Все три функции детерминированы: одинаковый прогон даёт одинаковый результат, порядок
строк задан сортировкой, а не порядком обхода.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations
from typing import Any, Iterable

#: группы изменений: человеческие подписи и правила отнесения пути
GROUPS: tuple[tuple[str, str], ...] = (
    ("RULES", "правила"),
    ("MODEL", "модели"),
    ("THRESHOLD", "пороги"),
    ("PRODUCT", "решение"),
    ("OFFERS", "офферы"),
    ("INPUT", "данные"),
)

#: префиксы путей, которые процедура сравнения считает входными данными
INPUT_PREFIXES = ("characteristics.", "ext_data.", "appData.")

GROUP_TITLES = dict(GROUPS)


def group_of(path: str) -> str:
    """К какой группе относится изменение пути. Чистая функция, тестируется отдельно."""
    if any(path.startswith(prefix) for prefix in INPUT_PREFIXES):
        return "INPUT"
    if ".rules." in path:
        return "RULES"
    if path.endswith(".cutOff") or ".cutOff." in path:
        return "THRESHOLD"
    if path.startswith("decisionResult.available_offers"):
        return "OFFERS"
    if path.endswith((".decision", ".rejectReason", ".rejectReasonCode")):
        return "PRODUCT"
    if ".scoring." in path or path.startswith("decisionResult.scoring"):
        return "MODEL"
    return "PRODUCT"


def _product_of(array_key: str) -> str:
    """Первый компонент ключа массива — продукт (``15210|AGELOW@21`` → ``15210``)."""
    return array_key.split("|", 1)[0] if array_key else ""


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = len(left | right)
    return round(len(left & right) / union, 3) if union else 0.0


class _Scan:
    """Один проход по изменённым строкам + слою разрезов."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.app_groups: dict[str, set[str]] = defaultdict(set)
        self.changed_apps: set[str] = set()
        self.rule_apps: dict[str, set[str]] = defaultdict(set)
        self.threshold_apps: dict[str, set[str]] = defaultdict(set)
        self.model_apps: dict[str, set[str]] = defaultdict(set)
        self.product_apps: dict[str, set[str]] = defaultdict(set)
        self.rule_names: dict[str, str] = {}
        self.decision_changed: dict[str, int] = {}
        self.input_changed: dict[str, bool] = {}
        self._loaded = False

    def load(self) -> "_Scan":
        if self._loaded:
            return self
        from ..db.mart import Mart

        mart = Mart(self.run_id)
        for row in mart.rows(diff_flag=1):
            app_id = row["APP_ID"]
            path = row["PATH"]
            self.changed_apps.add(app_id)
            group = group_of(path)
            self.app_groups[app_id].add(group)
            product = _product_of(row.get("ARRAY_KEY") or "")
            if group == "RULES":
                parts = (row.get("ARRAY_KEY") or "").split("|")
                name = parts[-1].strip() if len(parts) > 1 else (row.get("KEY") or path)
                self.rule_names[name] = name
                self.rule_apps[name].add(app_id)
            elif group == "THRESHOLD":
                self.threshold_apps[product or "—"].add(app_id)
            elif group == "MODEL":
                self.model_apps[product or "—"].add(app_id)
            elif group == "PRODUCT":
                if path == "decisionResult.decision":
                    continue          # итог по заявке, а не элемент стратегии
                self.product_apps[product or "заявка"].add(app_id)
            elif group == "INPUT":
                self.input_changed[app_id] = True

        for segment in mart.segments():
            app_id = segment["APP_ID"]
            self.decision_changed[app_id] = int(segment.get("DECISION_CHANGED") or 0)
            before = (segment.get("SCORE_OKB_BEFORE") or "")
            after = (segment.get("SCORE_OKB_AFTER") or "")
            if before != after:
                self.input_changed[app_id] = True
        self._loaded = True
        return self

    # -- производные величины ------------------------------------------------
    def flips(self, apps: Iterable[str]) -> int:
        return sum(1 for app in apps if self.decision_changed.get(app))

    def logic_flips(self, apps: Iterable[str]) -> int:
        """Смены решения, у которых внешние скоры не менялись."""
        return sum(1 for app in apps
                   if self.decision_changed.get(app) and not self.input_changed.get(app))


def _label(groups: set[str]) -> str:
    if len(groups) == 1:
        return GROUP_TITLES[next(iter(groups))]
    titles = sorted(GROUP_TITLES[group] for group in groups)
    return " + ".join(titles)


def archetypes(run_id: str, *, limit: int = 12) -> list[dict[str, Any]]:
    """Архетипы изменений: сколько заявок и смен решения на каждый состав изменений."""
    scan = _Scan(run_id).load()
    buckets: dict[str, set[str]] = defaultdict(set)
    for app_id, groups in scan.app_groups.items():
        buckets[_label(groups)].add(app_id)

    total = len(scan.changed_apps)
    out: list[dict[str, Any]] = []
    for label, apps in buckets.items():
        flips = scan.flips(apps)
        logic = scan.logic_flips(apps)
        example = sorted(apps)[0] if apps else ""
        out.append({
            "label": label,
            "groups": sorted(scan.app_groups[example]) if example else [],
            "apps": len(apps),
            "share": round(len(apps) / total, 4) if total else 0.0,
            "flips": flips,
            "logic_flips": logic,
            "logic_share": round(logic / flips, 4) if flips else 0.0,
            "example_app": sorted(apps)[0] if apps else "",
        })
    out.sort(key=lambda item: (-item["apps"], item["label"]))
    return out[:limit]


def element_impact(run_id: str, *, limit: int = 15) -> list[dict[str, Any]]:
    """Элементы стратегии, отсортированные по влиянию.

    Влияние = заявки, которых коснулось изменение, плюс удвоенные смены решения:
    изменение, которое переворачивает решения, важнее изменения косметического.
    """
    scan = _Scan(run_id).load()
    rows: list[dict[str, Any]] = []
    for kind, mapping, prefix in (("правило", scan.rule_apps, "правило"),
                                  ("порог", scan.threshold_apps, "порог продукта"),
                                  ("модель", scan.model_apps, "модель продукта"),
                                  ("продукт", scan.product_apps, "решение по продукту")):
        for name, apps in mapping.items():
            flips = scan.flips(apps)
            logic = scan.logic_flips(apps)
            rows.append({
                "kind": kind,
                "name": f"{prefix}: {name}" if kind != "правило" else f"{prefix} {name}",
                "apps": len(apps),
                "flips": flips,
                "logic_flips": logic,
                "logic_share": round(logic / flips, 4) if flips else 0.0,
                "impact": len(apps) + 2 * flips,
                "example_app": sorted(apps)[0] if apps else "",
            })
    rows.sort(key=lambda item: (-item["impact"], -item["flips"], item["name"]))
    return rows[:limit]


def cooccurrence(run_id: str, *, limit: int = 10, min_apps: int = 3) -> list[dict[str, Any]]:
    """Пары групп изменений, которые встречаются у одних и тех же заявок.

    Коэффициент Жаккара = |A ∩ B| / |A ∪ B|. Это **корреляция**: она показывает, что
    изменения идут вместе, и не доказывает, что одно вызвало другое.
    """
    scan = _Scan(run_id).load()
    by_group: dict[str, set[str]] = defaultdict(set)
    for app_id, groups in scan.app_groups.items():
        for group in groups:
            by_group[group].add(app_id)

    out: list[dict[str, Any]] = []
    for left, right in combinations(sorted(by_group), 2):
        both = by_group[left] & by_group[right]
        if len(both) < min_apps:
            continue
        out.append({
            "left": GROUP_TITLES[left],
            "right": GROUP_TITLES[right],
            "both": len(both),
            "jaccard": _jaccard(by_group[left], by_group[right]),
            "flips": scan.flips(both),
            "logic_flips": scan.logic_flips(both),
            "example_app": sorted(both)[0],
        })
    out.sort(key=lambda item: (-item["jaccard"], -item["both"]))
    return out[:limit]


def summary(run_id: str) -> dict[str, Any]:
    """Короткая сводка для верхней строки страницы."""
    scan = _Scan(run_id).load()
    changed = scan.changed_apps
    flips = [app for app in changed if scan.decision_changed.get(app)]
    logic = [app for app in flips if not scan.input_changed.get(app)]
    return {
        "apps_total": len(scan.decision_changed),
        "apps_changed": len(changed),
        "flips": len(flips),
        "logic_flips": len(logic),
        "input_flips": len(flips) - len(logic),
        "groups": {GROUP_TITLES[group]: len(apps)
                   for group, apps in sorted(
                       ((g, {a for a, gs in scan.app_groups.items() if g in gs})
                        for g, _ in GROUPS))},
    }
