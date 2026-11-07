"""Проверка контракта пользовательского кода **без исполнения** (только AST).

Кнопка «Проверить контракт» в рабочей области и тесты используют одну и ту же функцию —
поэтому «в интерфейсе зелено, а в тестах красно» невозможно по построению.

Что проверяется:

* код разбирается как Python (ошибка синтаксиса — с номером строки и колонки);
* для этапа ``check`` обязателен ``CHECK = {...}`` с непустыми ``id``, ``label`` и
  ``entrypoint``, а ``entrypoint`` должен быть функцией, определённой в этом же файле;
* для этапов ``normalize`` / ``compare`` / ``aggregate`` обязателен ``PROCEDURE = {...}``
  с ``id``, ``stage`` (совпадает с выбранным) и ``entrypoint``-функцией;
* для этапа ``normalize`` дополнительно принимается **ридер** — функция
  ``read_document(path)`` без объявления ``PROCEDURE``: именно так подключается свой формат
  файла (см. «Входной формат → Свой .py-ридер»).

Импорт и выполнение проверяет воркер в отдельном процессе: здесь намеренно ни одного
``exec``/``import`` пользовательского файла.
"""

from __future__ import annotations

import ast
from typing import Any

STAGE_KINDS = ("check", "normalize", "compare", "aggregate")

#: обязательные ключи в объявлении
REQUIRED = {
    "CHECK": ("id", "label", "entrypoint"),
    "PROCEDURE": ("id", "stage", "entrypoint"),
}


def _functions(tree: ast.Module) -> set[str]:
    return {node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _declaration(tree: ast.Module, name: str) -> ast.expr | None:
    """Найти верхнеуровневое присваивание ``NAME = {...}`` и вернуть правую часть."""
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return value
    return None


def _module_dicts(tree: ast.Module) -> dict[str, dict[str, Any]]:
    """Словари, объявленные на верхнем уровне: ``DEFAULTS = {...}``.

    Нужны, чтобы ``CHECK = {..., "params": DEFAULTS}`` считался корректным: параметры
    часто выносят в константу, и это нормальный стиль.
    """
    found: dict[str, dict[str, Any]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if (isinstance(target, ast.Name) and isinstance(node.value, ast.Dict)):
                parsed = _literal_dict(node.value, {})
                if parsed is not None:
                    found[target.id] = parsed
    return found


def _literal_dict(node: ast.expr | None,
                  module_dicts: dict[str, dict[str, Any]] | None = None
                  ) -> dict[str, Any] | None:
    """Разобрать словарь из литералов. Нелитеральные значения (функции) помечаем именем."""
    module_dicts = module_dicts or {}
    if isinstance(node, ast.Name) and node.id in module_dicts:
        return dict(module_dicts[node.id])
    if not isinstance(node, ast.Dict):
        return None
    out: dict[str, Any] = {}
    for key_node, value_node in zip(node.keys, node.values):
        if key_node is None:
            continue
        try:
            key = ast.literal_eval(key_node)
        except (ValueError, SyntaxError):
            continue
        if isinstance(value_node, ast.Name):
            if value_node.id in module_dicts:
                out[key] = dict(module_dicts[value_node.id])
            else:
                out[key] = ("name", value_node.id)
        elif isinstance(value_node, ast.Lambda):
            out[key] = ("lambda", "lambda")
        else:
            try:
                out[key] = ast.literal_eval(value_node)
            except (ValueError, SyntaxError):
                out[key] = ("expr", ast.unparse(value_node))
    return out


def _fail(message: str, *, kind: str = "", hint: str = "") -> dict[str, Any]:
    return {"ok": False, "kind": kind, "message": message, "hint": hint, "details": {}}


def validate_code(code: str, stage: str) -> dict[str, Any]:
    """Проверить контракт кода для выбранного этапа. Ничего не исполняет."""
    if stage not in STAGE_KINDS:
        return _fail(f"неизвестный этап {stage!r}; допустимые: {', '.join(STAGE_KINDS)}")
    source = code or ""
    if not source.strip():
        return _fail("пустой код: сначала вставьте пример или напишите свой",
                     hint="в блоке «Готовые примеры» есть пять рабочих заготовок")

    try:
        tree = ast.parse(source, filename="workspace.py")
    except SyntaxError as exc:
        where = f"строка {exc.lineno}" + (f", колонка {exc.offset}" if exc.offset else "")
        return _fail(f"синтаксическая ошибка ({where}): {exc.msg}")

    functions = _functions(tree)
    module_dicts = _module_dicts(tree)
    expects = "CHECK" if stage == "check" else "PROCEDURE"
    declared = _literal_dict(_declaration(tree, expects), module_dicts)

    if stage == "check":
        if declared is None:
            found = ", ".join(sorted(name for name in ("CHECK", "PROCEDURE")
                                     if _declaration(tree, name) is not None)) or "ничего"
            return _fail(f"нет объявления CHECK = {{...}} (найдено: {found})",
                         kind="check",
                         hint="контракт проверки: CHECK = {'id', 'label', 'params', "
                              "'entrypoint'}")
        return _validate_check(declared, functions)

    if declared is not None:
        return _validate_procedure(declared, functions, stage)

    # ридер — допустимая альтернатива только для нормализации
    if stage == "normalize" and "read_document" in functions:
        return {"ok": True, "kind": "reader",
                "message": "это ридер: read_document(path) → dict. Подключается в "
                           "конфигурации как «Свой .py-ридер», отдельного PROCEDURE не нужно.",
                "hint": "", "details": {"function": "read_document"}}

    return _fail(f"нет объявления PROCEDURE = {{...}} для этапа «{stage}»",
                 kind="procedure",
                 hint="контракт процедуры: PROCEDURE = {'id', 'stage', 'params', "
                      "'entrypoint'}; для нормализации допускается ридер read_document(path)")


def _validate_check(declared: dict[str, Any], functions: set[str]) -> dict[str, Any]:
    missing = [key for key in REQUIRED["CHECK"] if key not in declared]
    if missing:
        return _fail(f"в CHECK не хватает полей: {', '.join(missing)}", kind="check",
                     hint="обязательные поля: " + ", ".join(REQUIRED["CHECK"]))
    for key in ("id", "label"):
        value = declared.get(key)
        if not isinstance(value, str) or not value.strip():
            return _fail(f"CHECK[{key!r}] должен быть непустой строкой", kind="check")
    entry = declared.get("entrypoint")
    if not isinstance(entry, tuple):
        return _fail("CHECK['entrypoint'] должен ссылаться на функцию по имени",
                     kind="check", hint="например: entrypoint = run")
    if entry[0] == "name" and entry[1] not in functions:
        return _fail(f"функция {entry[1]!r} объявлена в CHECK, но не определена в файле",
                     kind="check", hint="добавьте def " + entry[1] + "(mart, params=None)")
    params = declared.get("params")
    if params is not None and not isinstance(params, dict):
        return _fail("CHECK['params'] должен быть словарём", kind="check")
    return {"ok": True, "kind": "check",
            "message": f"проверка «{declared['label']}» (id: {declared['id']}), "
                       f"вердикт-функция: {entry[1]}",
            "hint": "импорт, поля строки проверки и выполнение проверит воркер при запуске",
            "details": {"id": declared["id"], "label": declared["label"],
                        "params": params if isinstance(params, dict) else {},
                        "entrypoint": entry[1]}}


def _validate_procedure(declared: dict[str, Any], functions: set[str],
                        stage: str) -> dict[str, Any]:
    missing = [key for key in REQUIRED["PROCEDURE"] if key not in declared]
    if missing:
        return _fail(f"в PROCEDURE не хватает полей: {', '.join(missing)}",
                     kind="procedure",
                     hint="обязательные поля: " + ", ".join(REQUIRED["PROCEDURE"]))
    declared_stage = declared.get("stage")
    if declared_stage != stage:
        return _fail(f"PROCEDURE['stage'] = {declared_stage!r}, а выбран этап {stage!r}",
                     kind="procedure",
                     hint="смените тип в селекторе «Тип» или поправьте stage в объявлении")
    label = declared.get("id")
    if not isinstance(label, str) or not label.strip():
        return _fail("PROCEDURE['id'] должен быть непустой строкой", kind="procedure")
    entry = declared.get("entrypoint")
    if not isinstance(entry, tuple):
        return _fail("PROCEDURE['entrypoint'] должен ссылаться на функцию по имени",
                     kind="procedure")
    if entry[0] == "name" and entry[1] not in functions:
        return _fail(f"функция {entry[1]!r} объявлена в PROCEDURE, но не определена в файле",
                     kind="procedure")
    return {"ok": True, "kind": "procedure",
            "message": f"процедура «{label}» для этапа «{stage}», функция: {entry[1]}",
            "hint": "импорт и выполнение проверит воркер при запуске",
            "details": {"id": label, "stage": stage, "entrypoint": entry[1]}}
