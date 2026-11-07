"""Агрегация по последствиям: архетипы, влияние элементов, совместные изменения.

Тест строит маленькую витрину вручную, поэтому числа в нём проверяются точно и не
зависят от демонстрационного прогона: у каждой заявки заранее известен состав
изменений и то, менялось ли решение.
"""

from __future__ import annotations

import pytest

from change_analysis.services import insights

pytestmark = pytest.mark.integration


@pytest.fixture()
def mart(synthetic_run):
    """Четыре заявки с разным составом изменений.

    A1 — изменилось правило, решение перевернулось, входные данные те же (логика);
    A2 — изменились только внешние скоры, решение перевернулось (данные);
    A3 — изменились модель и порог, решения не менялись;
    A4 — изменений нет (только SAME).
    """
    from qa_support import comparison_row as row, segment_row as segment

    rows = [
        # A1: правило + офферы, решение изменилось
        row("", "A1", "decisionResult.product_trials.rules.hit", "15210|AGELOW@21",
            "false", "true", "VALUE_CHANGED"),
        row("", "A1", "decisionResult.decision", "", "APPROVE", "REJECT", "VALUE_CHANGED"),
        row("", "A1", "decisionResult.available_offers.product_id", "15210|standard",
            "15210", None, "ATTRIBUTE_LOST"),
        # A2: только входные данные
        row("", "A2", "characteristics.score_okb", "", "700", "650", "VALUE_CHANGED"),
        row("", "A2", "decisionResult.decision", "", "APPROVE", "REJECT", "VALUE_CHANGED"),
        # A3: модель и порог, без смены решения
        row("", "A3", "decisionResult.product_trials.scoring.pd.modelName", "751",
            "MODEL_A", "MODEL_B", "VALUE_CHANGED"),
        row("", "A3", "decisionResult.product_trials.cutOff.value", "751",
            "0.55", "0.62", "VALUE_CHANGED"),
        # A4: без изменений
        row("", "A4", "decisionResult.decision", "", "APPROVE", "APPROVE", "SAME"),
    ]
    segments = [
        segment("", "A1", "APPROVE", "REJECT", score_okb_before="700", score_okb_after="700"),
        segment("", "A2", "APPROVE", "REJECT", score_okb_before="700", score_okb_after="650"),
        segment("", "A3", "APPROVE", "APPROVE"),
        segment("", "A4", "APPROVE", "APPROVE"),
    ]
    return synthetic_run("qa-insights", rows=rows, segments=segments)


def test_summary_separates_logic_from_input_data(mart: str) -> None:
    summary = insights.summary(mart)
    assert summary["apps_total"] == 4
    assert summary["apps_changed"] == 3
    assert summary["flips"] == 2
    assert summary["logic_flips"] == 1          # A1: скоры не менялись
    assert summary["input_flips"] == 1          # A2: скоры изменились


def test_archetypes_group_by_composition_of_changes(mart: str) -> None:
    items = {item["label"]: item for item in insights.archetypes(mart)}
    assert sum(item["apps"] for item in items.values()) == 3
    rules = items["офферы + правила + решение"]
    assert rules["apps"] == 1 and rules["flips"] == 1 and rules["logic_share"] == 1.0
    data = items["данные + решение"]
    assert data["apps"] == 1 and data["logic_share"] == 0.0
    models = items["модели + пороги"]
    assert models["apps"] == 1 and models["flips"] == 0


def test_element_impact_names_elements_not_paths(mart: str) -> None:
    items = insights.element_impact(mart)
    names = {item["name"] for item in items}
    assert "правило AGELOW@21" in names
    assert "модель продукта: 751" in names
    assert "порог продукта: 751" in names
    impacts = [item["impact"] for item in items]
    assert impacts == sorted(impacts, reverse=True)
    # итоговое решение по заявке не является «элементом стратегии»
    assert all("решение по продукту: заявка" != item["name"] for item in items)


def test_cooccurrence_finds_pairs_but_warns_about_correlation(mart: str) -> None:
    items = insights.cooccurrence(mart, min_apps=1)
    pairs = {(item["left"], item["right"]): item for item in items}
    assert ("офферы", "правила") in pairs
    assert pairs[("офферы", "правила")]["both"] == 1
    assert pairs[("офферы", "правила")]["jaccard"] == 1.0
    assert ("данные", "правила") not in pairs      # вместе не встречались


def test_results_are_deterministic(mart: str) -> None:
    assert insights.archetypes(mart) == insights.archetypes(mart)
    assert insights.element_impact(mart) == insights.element_impact(mart)
    assert insights.cooccurrence(mart, min_apps=1) == insights.cooccurrence(mart, min_apps=1)


def test_group_of_maps_paths_to_strategy_parts() -> None:
    cases = {
        "decisionResult.product_trials.rules.hit": "RULES",
        "decisionResult.product_trials.cutOff.value": "THRESHOLD",
        "decisionResult.product_trials.decision": "PRODUCT",
        "decisionResult.available_offers.rank": "OFFERS",
        "decisionResult.scoring.pd.modelName": "MODEL",
        "decisionResult.product_trials.scoring.scoreModels.value": "MODEL",
        "characteristics.score_okb": "INPUT",
        "ext_data.status": "INPUT",
        "something.unexpected": "PRODUCT",
    }
    for path, expected in cases.items():
        assert insights.group_of(path) == expected, path
