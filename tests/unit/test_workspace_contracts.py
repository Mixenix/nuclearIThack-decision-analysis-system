"""Контракты рабочей области: примеры обязаны проходить ту же проверку, что и кнопка.

Кнопка «Проверить контракт» вызывает ``services.workspace.validate_code`` — ровно эту
функцию тестируем здесь. Если пример перестанет проходить, тест упадёт раньше, чем это
увидит человек в интерфейсе.
"""

from __future__ import annotations

import pytest

from change_analysis.services.workspace import STAGE_KINDS, validate_code
from change_analysis.ui.workspace_examples import EXAMPLES, by_key

pytestmark = pytest.mark.unit


# --- все примеры проходят проверку ----------------------------------------

@pytest.mark.parametrize("example", EXAMPLES, ids=[item["key"] for item in EXAMPLES])
def test_example_passes_the_contract_button(example: dict) -> None:
    verdict = validate_code(example["code"], example["stage"])
    assert verdict["ok"], f"{example['title']}: {verdict['message']}"
    assert verdict["kind"] in ("check", "procedure", "reader")


def test_every_stage_has_at_least_one_example() -> None:
    stages = {item["stage"] for item in EXAMPLES}
    assert "check" in stages
    assert "normalize" in stages


def test_examples_are_distinct_and_described() -> None:
    keys = [item["key"] for item in EXAMPLES]
    assert len(keys) == len(set(keys)), "ключи примеров должны быть уникальны"
    for item in EXAMPLES:
        assert item["title"].strip()
        assert item["about"].strip()
        assert isinstance(item["params"], dict)


# --- проверка не исполняет код --------------------------------------------

def test_validation_does_not_execute_user_code(tmp_path) -> None:
    """Верхнеуровневый код не должен выполняться при проверке контракта."""
    marker = tmp_path / "executed.marker"
    code = (
        f"open({str(marker)!r}, 'w').write('x')\n"
        "def run(mart, params=None):\n    return []\n"
        "CHECK = {'id': 'loud', 'label': 'gromkaya', 'params': {}, 'entrypoint': run}\n"
    )
    verdict = validate_code(code, "check")
    assert verdict["ok"]
    assert not marker.exists(), "проверка контракта исполнила пользовательский код"


# --- отказы, которые обязан ловить валидатор ------------------------------

@pytest.mark.parametrize("code,stage,expected", [
    ("", "check", "пустой код"),
    ("def run(x): return x", "check", "нет объявления CHECK"),
    ("CHECK = {", "check", "синтаксическая ошибка"),
    ("def run(m): return []\nCHECK = {'id': '', 'label': 'x', 'entrypoint': run}",
     "check", "CHECK['id']"),
    ("def run(m): return []\nCHECK = {'id': 'a', 'label': 'b'}",
     "check", "не хватает полей"),
    ("CHECK = {'id': 'a', 'label': 'b', 'entrypoint': missing}",
     "check", "не определена в файле"),
    ("def run(m): return []\nCHECK = {'id': 'a', 'label': 'b', 'entrypoint': run,"
     " 'params': 3}", "check", "params"),
    ("def run(c, i, p): return None\nPROCEDURE = {'id': 'p', 'stage': 'compare',"
     " 'entrypoint': run}", "normalize", "выбран этап"),
    ("def run(c, i, p): return None", "normalize", "нет объявления PROCEDURE"),
    ("PROCEDURE = {'id': 'x'}", "aggregate", "не хватает полей"),
    ("CHECK = {'id': 'a', 'label': 'b', 'entrypoint': run}", "nonsense", "неизвестный этап"),
])
def test_invalid_code_is_rejected(code: str, stage: str, expected: str) -> None:
    verdict = validate_code(code, stage)
    assert not verdict["ok"], verdict
    assert expected in verdict["message"], verdict["message"]


def test_reader_is_accepted_for_normalize_only() -> None:
    code = "def read_document(path):\n    return {}\n"
    assert validate_code(code, "normalize")["ok"]
    assert validate_code(code, "normalize")["kind"] == "reader"
    verdict = validate_code(code, "aggregate")
    assert not verdict["ok"]
    assert "PROCEDURE" in verdict["message"]


def test_params_may_reference_a_module_level_dict() -> None:
    code = ("MY = {'limit': 3}\n"
            "def run(mart, params=None):\n    return []\n"
            "CHECK = {'id': 'a', 'label': 'b', 'params': MY, 'entrypoint': run}\n")
    verdict = validate_code(code, "check")
    assert verdict["ok"], verdict
    assert verdict["details"]["params"] == {"limit": 3}


def test_validator_knows_all_stages() -> None:
    assert set(STAGE_KINDS) == {"check", "normalize", "compare", "aggregate"}


def test_the_reader_example_declares_its_purpose() -> None:
    """Пример-ридер обязан упоминать контракт read_document, а не выглядеть поломкой."""
    example = by_key("reader_custom_format")
    assert example is not None
    verdict = validate_code(example["code"], example["stage"])
    assert verdict["ok"] and verdict["kind"] == "reader"
    assert "read_document" in verdict["message"]
