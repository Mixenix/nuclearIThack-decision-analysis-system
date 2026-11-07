#!/usr/bin/env python3
"""Карта кода: граф модулей, слои, циклы и отклонения от архитектуры.

Нужна, чтобы быстро понять проект перед правкой и чтобы ревьюер видел структуру, а не
догадывался по именам файлов. Ничего не запускает и не меняет: разбирает исходники
через ``ast``.

    python3 tools/codegraph.py                       # отчёт в stdout
    python3 tools/codegraph.py --out /tmp/codegraph  # + markdown, json, mermaid
    python3 tools/codegraph.py --json                # только машинный вид

Что считается:

* модуль = файл ``.py``; ребро = внутренний импорт;
* слой = первый каталог внутри ``change_analysis``;
* страницы — функции с декоратором ``@ui.page`` (маршрут видно в отчёте);
* циклы ищутся алгоритмом Тарьяна;
* отклонения — рёбра, запрещённые правилами слоёв (список в ``LAYER_RULES``).
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
SRC = APP / "src" / "change_analysis"

#: каким слоям что разрешено импортировать (по первому каталогу внутри пакета)
LAYER_RULES: dict[str, set[str]] = {
    "contracts": set(),
    "config": set(),
    "pipeline": {"contracts"},
    "procedures": {"pipeline", "contracts"},
    "checks": {"pipeline", "contracts", "config"},
    "db": {"config", "contracts"},
    "reports": {"contracts", "db", "services"},
    "llm": {"config", "contracts"},
    "auth": {"db", "config"},
    "services": {"contracts", "config", "db", "pipeline", "procedures", "checks",
                 "reports", "llm", "auth"},
    "worker": {"contracts", "config", "db", "pipeline", "procedures", "checks",
               "services"},
    "ui": {"contracts", "config", "services", "pipeline", "checks", "llm", "reports",
           "auth", "db"},
    "cli": {"contracts", "config", "services", "worker", "auth", "db", "checks",
            "procedures", "pipeline"},
}

ENTRY_POINTS = {"main.py", "src/change_analysis/worker/main.py",
                "src/change_analysis/cli.py"}


def module_name(path: Path) -> str:
    rel = path.relative_to(SRC).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(["change_analysis", *parts]) if parts else "change_analysis"


def layer_of(name: str) -> str:
    parts = name.split(".")
    return parts[1] if len(parts) > 1 else "(корень)"


def parse_module(path: Path) -> dict:
    """Разбор модуля: импорты (обычные и «ленивые»), маршруты, размер."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    name = module_name(path)
    is_package = path.name == "__init__.py"
    imports: set[str] = set()
    lazy: set[str] = set()
    routes: list[str] = []
    counts = {"classes": 0, "functions": 0}

    def add_import(node: ast.AST, *, lazy_context: bool) -> None:
        target_set = lazy if lazy_context else imports
        base = name.split(".")
        if isinstance(node, ast.ImportFrom):
            if node.level:
                cut = len(base) - node.level + (1 if is_package else 0)
                prefix = base[:cut]
            elif node.module and node.module.startswith("change_analysis"):
                prefix = node.module.split(".")
            else:
                return
            if node.module:
                prefix = prefix + node.module.split(".")
            target = ".".join(prefix)
            target_set.add(target)
            # "from .services import runs" — конкретный модуль важнее пакета
            for alias in node.names:
                target_set.add(f"{target}.{alias.name}")
        else:
            for alias in node.names:
                if alias.name.startswith("change_analysis"):
                    target_set.add(alias.name)

    def walk(node: ast.AST, *, lazy_context: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            counts["functions"] += 1
            for decorator in node.decorator_list:
                text = ast.unparse(decorator)
                if text.startswith("ui.page("):
                    route = text[len("ui.page("):].split(")")[0].strip()
                    routes.append(route.strip("'\""))
            for child in node.body:
                walk(child, lazy_context=True)
            return
        if isinstance(node, ast.ClassDef):
            counts["classes"] += 1
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            add_import(node, lazy_context=lazy_context)
        for child in ast.iter_child_nodes(node):
            walk(child, lazy_context=lazy_context)

    walk(tree, lazy_context=False)
    return {
        "module": name,
        "path": str(path.relative_to(APP)),
        "layer": layer_of(name),
        "lines": source.count("\n") + 1,
        "classes": counts["classes"],
        "functions": counts["functions"],
        "routes": routes,
        "imports": sorted(imports),
        "lazy_imports": sorted(lazy - imports),
    }


def resolve(target: str, modules: dict[str, dict]) -> str | None:
    """Импорт может указывать на пакет — поднимаемся до существующего модуля."""
    candidate = target
    while candidate:
        if candidate in modules:
            return candidate
        if "." not in candidate:
            return None
        candidate = candidate.rsplit(".", 1)[0]
    return None


def strongly_connected(nodes: list[str], edges: dict[str, set[str]]) -> list[list[str]]:
    """Циклы: компоненты сильной связности размера > 1 (алгоритм Тарьяна)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: list[list[str]] = []
    counter = [0]

    def visit(node: str) -> None:
        index[node] = low[node] = counter[0]
        counter[0] += 1
        stack.append(node)
        on_stack.add(node)
        for neighbour in sorted(edges.get(node, ())):
            if neighbour not in index:
                visit(neighbour)
                low[node] = min(low[node], low[neighbour])
            elif neighbour in on_stack:
                low[node] = min(low[node], index[neighbour])
        if low[node] == index[node]:
            component = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1:
                result.append(sorted(component))

    for node in nodes:
        if node not in index:
            visit(node)
    return result


def build() -> dict:
    modules = {module_name(path): parse_module(path) for path in sorted(SRC.rglob("*.py"))}
    edges: dict[str, set[str]] = {}
    unresolved: dict[str, list[str]] = {}
    for name, info in modules.items():
        resolved = set()
        for target in info["imports"]:
            hit = resolve(target, modules)
            if hit and hit != name:
                resolved.add(hit)
            elif hit is None:
                unresolved.setdefault(name, []).append(target)
        lazy_targets = set()
        for target in info.get("lazy_imports", []):
            hit = resolve(target, modules)
            if hit and hit != name:
                lazy_targets.add(hit)
        info["lazy_resolved"] = sorted(lazy_targets - resolved)
        edges[name] = resolved
        info["resolved"] = sorted(resolved)

    fan_in: dict[str, int] = defaultdict(int)
    for sources in edges.values():
        for target in sources:
            fan_in[target] += 1
    for name, info in modules.items():
        info["fan_in"] = fan_in.get(name, 0)
        info["fan_out"] = len(edges[name])

    violations: list[dict] = []
    for name, info in modules.items():
        allowed = LAYER_RULES.get(info["layer"], set())
        for target in edges[name]:
            target_layer = modules[target]["layer"]
            if target_layer != info["layer"] and target_layer not in allowed:
                violations.append({"from": name, "from_layer": info["layer"],
                                   "to": target, "to_layer": target_layer})

    imported = set(fan_in)
    roots = sorted(name for name in modules
                   if modules[name]["fan_in"] == 0
                   and modules[name]["path"] not in ENTRY_POINTS)

    return {
        "modules": modules,
        "edges": {name: sorted(targets) for name, targets in edges.items()},
        "cycles": strongly_connected(sorted(modules), edges),
        "violations": sorted(violations, key=lambda item: (item["from"], item["to"])),
        "unresolved": unresolved,
        "roots": roots,
        "routes": sorted((route, info["module"]) for info in modules.values()
                         for route in info["routes"]),
    }


def layer_table(graph: dict) -> list[dict]:
    layers: dict[str, dict] = defaultdict(lambda: {"modules": 0, "lines": 0,
                                                   "fan_in": 0, "fan_out": 0})
    for info in graph["modules"].values():
        row = layers[info["layer"]]
        row["modules"] += 1
        row["lines"] += info["lines"]
        row["fan_in"] += info["fan_in"]
        row["fan_out"] += info["fan_out"]
    return [{"layer": layer, **values}
            for layer, values in sorted(layers.items(), key=lambda kv: -kv[1]["lines"])]


def mermaid(graph: dict) -> str:
    """Схема слоёв для просмотра в любом markdown-просмотрщике."""
    layers = [row["layer"] for row in layer_table(graph)]
    lines = ["```mermaid", "graph LR"]
    for layer in layers:
        lines.append(f"  {layer.replace('(', '').replace(')', '')}[{layer}]")
    links = set()
    for name, targets in graph["edges"].items():
        source_layer = graph["modules"][name]["layer"]
        for target in targets:
            target_layer = graph["modules"][target]["layer"]
            if source_layer != target_layer:
                links.add((source_layer, target_layer))
    for source, target in sorted(links):
        lines.append(f"  {source} --> {target}")
    lines.append("```")
    return "\n".join(lines)


def report(graph: dict) -> str:
    out: list[str] = []
    total_modules = len(graph["modules"])
    total_lines = sum(info["lines"] for info in graph["modules"].values())
    out.append(f"# Карта кода: {total_modules} модулей, {total_lines} строк\n")

    out.append("## Слои\n")
    out.append("| Слой | Модулей | Строк | Импортируют его | Импортирует он |")
    out.append("|---|---|---|---|---|")
    for row in layer_table(graph):
        out.append(f"| {row['layer']} | {row['modules']} | {row['lines']} | "
                   f"{row['fan_in']} | {row['fan_out']} |")

    out.append("\n## Самые связанные модули (на них ссылаются чаще всего)\n")
    out.append("| Модуль | Слой | Ссылок | Импортов | Строк |")
    out.append("|---|---|---|---|---|")
    hubs = sorted(graph["modules"].values(), key=lambda item: (-item["fan_in"],
                                                               -item["lines"]))[:12]
    for info in hubs:
        out.append(f"| `{info['module']}` | {info['layer']} | {info['fan_in']} | "
                   f"{info['fan_out']} | {info['lines']} |")

    out.append("\n## Маршруты интерфейса\n")
    if graph["routes"]:
        out.append("| Маршрут | Модуль |")
        out.append("|---|---|")
        for route, module in graph["routes"]:
            out.append(f"| `{route}` | `{module}` |")
    else:
        out.append("маршрутов не найдено")

    out.append("\n## Циклы\n")
    if graph["cycles"]:
        for component in graph["cycles"]:
            out.append("* " + " → ".join(f"`{name}`" for name in component))
    else:
        out.append("циклов нет")

    out.append("\n## Отклонения от правил слоёв\n")
    if graph["violations"]:
        out.append("| Откуда | Слой | Куда | Слой цели |")
        out.append("|---|---|---|---|")
        for item in graph["violations"]:
            out.append(f"| `{item['from']}` | {item['from_layer']} | "
                       f"`{item['to']}` | {item['to_layer']} |")
    else:
        out.append("отклонений нет")

    out.append("\n## Модули без импортёров\n")
    out.append(", ".join(f"`{name}`" for name in graph["roots"]) or "нет")

    out.append("\n## Схема слоёв\n")
    out.append(mermaid(graph))
    out.append("")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="Карта кода проекта")
    parser.add_argument("--out", default="", help="каталог для codegraph.md/.json/.mmd")
    parser.add_argument("--json", action="store_true", help="только машинный вид")
    args = parser.parse_args()

    graph = build()
    text = report(graph)
    if args.json:
        print(json.dumps(graph, ensure_ascii=False, indent=1))
    else:
        print(text)
    if args.out:
        target = Path(args.out)
        target.mkdir(parents=True, exist_ok=True)
        (target / "codegraph.md").write_text(text, encoding="utf-8")
        (target / "codegraph.json").write_text(
            json.dumps(graph, ensure_ascii=False, indent=1), encoding="utf-8")
        (target / "codegraph.mmd").write_text(mermaid(graph).strip("`").strip(), encoding="utf-8")
        print(f"\nзаписано в {target}: codegraph.md, codegraph.json, codegraph.mmd")
    return 0


if __name__ == "__main__":
    sys.exit(main())
