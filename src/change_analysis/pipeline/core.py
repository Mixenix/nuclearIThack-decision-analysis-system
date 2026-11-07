"""Core normalization / comparison engine.

Pure-Python, dependency-free (stdlib only).  The same functions are used by the
worker subprocess (CPU or GPU-accelerated) and by the unit tests.

Design constraints taken from the task package (docs/ФОРМАТ_ОТВЕТА.md,
docs/СХЕМА_ВИТРИНЫ.md and PLAN.md §8-§10):

* one row  = one leaf attribute of one application in one slice;
* row key  = (app_id, source, path, array_key);
* `path`   = dotted path with every array marker removed;
* `array_key` = the element keys of *all* arrays on the path, outer -> inner,
  joined with "|".  Scalar arrays use the value itself, vector arrays use the
  positional index.

Nothing in this module knows about СПР-specific field *names* — the field names
live in the user procedure (see ``procedures/spr_normalize.py``).  This module
only knows the generic contracts: an ``ArraySpec`` table and an exclusion list.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

SEP = "|"

# ---------------------------------------------------------------------------
# value serialization
# ---------------------------------------------------------------------------

NONE_TOKEN = "NULL"


def serialize(value: Any) -> tuple[str, str]:
    """Return ``(value_str, value_type)``.

    The textual form is what the mart and the delivered checks compare against,
    so it is part of the public contract:

    * ``True`` / ``False`` -> ``"true"`` / ``"false"``  (JSON spelling, the
      delivered check ``check_stop_factor_disappeared`` compares against these)
    * ``None`` -> ``"NULL"``  (the delivered checks expect the text ``NULL``)
    * ``int`` -> decimal digits
    * ``float`` -> shortest round-trip repr; integral floats collapse to ``"1"``
      so that ``1`` and ``1.0`` compare equal (PLAN.md §8.4)
    * ``str`` -> unchanged (a literal ``"NULL"`` string stays ``"NULL"`` but is
      typed ``string`` so it never mixes with a real null)
    """
    if value is None:
        return NONE_TOKEN, "null"
    if isinstance(value, bool):
        return ("true" if value else "false"), "boolean"
    if isinstance(value, int):
        return str(value), "number"
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN", "number"
        if math.isinf(value):
            return ("Infinity" if value > 0 else "-Infinity"), "number"
        if value.is_integer() and abs(value) < 1e16:
            return str(int(value)), "number"
        return repr(value), "number"
    if isinstance(value, str):
        return value, "string"
    # Anything else is a container that should have been walked already.
    raise TypeError(f"not a scalar leaf: {type(value)!r}")


def escape_key(part: str) -> str:
    """Escape the ``|`` separator inside a single array key component."""
    return part.replace("\\", "\\\\").replace(SEP, "\\|")


def join_array_keys(parts: Sequence[str]) -> str:
    return SEP.join(escape_key(p) for p in parts)


# ---------------------------------------------------------------------------
# array configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArraySpec:
    """How one array on one path is matched between the slices.

    ``mode``:
      * ``"field"``  -> element identified by one or more fields (composite key
        when ``key_fields`` holds more than one entry);
      * ``"value"``  -> array of scalars, the value itself is the key, order is
        irrelevant;
      * ``"index"``  -> a positional vector, the index is the key.

    ``key_fields`` entries may be dotted paths *inside* the element, e.g.
    ``"body.action"`` for ``ext_data[]``.
    """

    path: str
    mode: str
    key_fields: tuple[str, ...] = ()

    def element_parts(self, element: Any, where: str) -> tuple[str, ...]:
        """Raw key parts of one element.

        Escaping happens exactly once, when the parts are assembled into
        ``ARRAY_KEY``; escaping here as well would turn the separator itself into
        ``\\|`` and break ``"<product_id>|<rule_name>"`` that the delivered check
        relies on.
        """
        if self.mode == "index":
            raise ValueError("index mode does not use element_parts()")
        if self.mode == "value":
            if isinstance(element, (dict, list)):
                raise ValueError(
                    f"array {where!r} is declared as value-keyed but holds a "
                    f"{type(element).__name__} element"
                )
            text, _ = serialize(element)
            return (text,)
        parts: list[str] = []
        for dotted in self.key_fields:
            node = element
            for piece in dotted.split("."):
                if isinstance(node, Mapping) and piece in node:
                    node = node[piece]
                else:
                    raise ValueError(
                        f"array {where!r}: element has no key field {dotted!r} "
                        f"(missing segment {piece!r})"
                    )
            if isinstance(node, (dict, list)):
                raise ValueError(
                    f"array {where!r}: key field {dotted!r} is not a scalar"
                )
            text, _ = serialize(node)
            parts.append(text)
        return tuple(parts)


DEFAULT_ARRAY_SPECS: tuple[ArraySpec, ...] = (
    ArraySpec("decisionResult.available_offers", "field", ("product_id", "type")),
    ArraySpec("decisionResult.product_trials", "field", ("product_id",)),
    ArraySpec("decisionResult.product_trials.rules", "field", ("name",)),
    ArraySpec("decisionResult.product_trials.scoring.scoreModels", "field", ("modelName",)),
    ArraySpec("decisionResult.scoring", "field", ("modelName",)),
    ArraySpec("decisionResult.scoring.value", "index"),
    ArraySpec("ext_data", "field", ("body.action",)),
    ArraySpec("product_catalog", "field", ("mfo_id", "product_id")),
    ArraySpec("tariff_matrix", "field", ("mfo_id", "product_id")),
)

#: manifest.json -> technical_fields_always_differ.  Configurable, never a
#: hard-coded constant of the kernel (task card: "список должен быть настройкой").
DEFAULT_EXCLUDED_PATHS: tuple[str, ...] = (
    "appData.request_id",
    "appData.createDateTime",
)


class NormalizationError(ValueError):
    """A structural problem in the input document (clear, actionable)."""


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------


@dataclass
class NormalizedRow:
    """One leaf attribute of one application in one slice."""

    app_id: str
    source: str          # BEFORE | AFTER
    prefix: str
    cls: str
    suffix: str
    array_key: str
    key: str
    path: str
    value: str
    value_type: str
    adjusted_value: str
    value_type_raw: str = ""
    pointer: str = ""    # JSON pointer to the original node, for explanations


def _split_path(path: str) -> tuple[str, str, str, str]:
    """``PREFIX, CLASS, SUFFIX, KEY`` for a dotted path.

    ``PREFIX`` = first segment, ``CLASS`` = second segment, ``KEY`` = last
    segment, ``SUFFIX`` = everything between them (dotted).  Short paths leave
    the middle parts empty, which is what makes
    ``PREFIX.CLASS.SUFFIX.KEY == PATH`` hold for every path length.
    """
    segments = path.split(".")
    key = segments[-1]
    prefix = segments[0]
    if len(segments) == 1:
        return prefix, "", "", key
    cls = segments[1] if len(segments) > 2 else ""
    suffix = ".".join(segments[2:-1])
    return prefix, cls, suffix, key


def normalize_document(
    doc: Mapping[str, Any],
    source: str,
    app_id: str,
    array_specs: Iterable[ArraySpec] = DEFAULT_ARRAY_SPECS,
    excluded_paths: Iterable[str] = DEFAULT_EXCLUDED_PATHS,
    adjust: Callable[[str, str, str], str] | None = None,
) -> list[NormalizedRow]:
    """Flatten one СПР response into normalized rows.

    ``adjust(path, value_str, value_type) -> adjusted_value`` implements
    bucketing; it must be deterministic and identical for both slices, which is
    guaranteed by the caller passing one function for the whole run.
    """
    specs = {spec.path: spec for spec in array_specs}
    excluded = set(excluded_paths)
    rows: list[NormalizedRow] = []
    seen: dict[tuple[str, str], int] = {}

    def emit(path: str, array_key: str, value: Any, pointer: str,
             key_override: str | None = None) -> None:
        if path in excluded:
            return
        text, vtype = serialize(value)
        k = (path, array_key)
        if k in seen:
            seen[k] += 1
            raise NormalizationError(
                f"duplicate row key in app {app_id} slice {source}: "
                f"PATH={path!r} ARRAY_KEY={array_key!r} occurs {seen[k]} times. "
                f"This is a data-quality problem; it is not silently de-duplicated."
            )
        seen[k] = 1
        prefix, cls, suffix, key = _split_path(path)
        if key_override is not None:
            key = key_override
        rows.append(
            NormalizedRow(
                app_id=app_id,
                source=source,
                prefix=prefix,
                cls=cls,
                suffix=suffix,
                array_key=array_key,
                key=key,
                path=path,
                value=text,
                value_type=vtype,
                adjusted_value=adjust(path, text, vtype) if adjust else text,
                value_type_raw=vtype,
                pointer=pointer,
            )
        )

    def walk(node: Any, path: tuple[str, ...], arrkeys: tuple[str, ...], ptr: str) -> None:
        if isinstance(node, Mapping):
            for k, v in node.items():
                walk(v, path + (k,), arrkeys, f"{ptr}/{k}")
            return
        if isinstance(node, list):
            dotted = ".".join(path)
            spec = specs.get(dotted)
            if spec is None:
                raise NormalizationError(
                    f"unknown array at PATH={dotted!r} (app {app_id}, slice "
                    f"{source}, pointer {ptr}). Add an array rule for this path "
                    f"instead of silently comparing by position."
                )
            if spec.mode == "index":
                for idx, element in enumerate(node):
                    walk(element, path, arrkeys + (str(idx),), f"{ptr}/{idx}")
                return
            if spec.mode == "value":
                for idx, element in enumerate(node):
                    parts = spec.element_parts(element, dotted)
                    emit(dotted, join_array_keys(arrkeys + parts), element,
                         f"{ptr}/{idx}", key_override="")
                return
            for idx, element in enumerate(node):
                eptr = f"{ptr}/{idx}"
                parts = spec.element_parts(element, dotted)
                walk(element, path, arrkeys + parts, eptr)
            return
        # scalar leaf
        dotted = ".".join(path)
        emit(dotted, join_array_keys(arrkeys), node, ptr)

    walk(doc, (), (), "")
    return rows


# ---------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------

SAME = "SAME"
VALUE_CHANGED = "VALUE_CHANGED"
ATTRIBUTE_NEW = "ATTRIBUTE_NEW"
ATTRIBUTE_LOST = "ATTRIBUTE_LOST"

CHANGE_TYPES = (SAME, VALUE_CHANGED, ATTRIBUTE_NEW, ATTRIBUTE_LOST)


@dataclass
class ComparisonRow:
    app_id: str
    prefix: str
    cls: str
    suffix: str
    array_key: str
    key: str
    path: str
    value_before: str | None
    value_after: str | None
    adjusted_before: str | None
    adjusted_after: str | None
    diff_flag: int
    change_type: str
    present_before: bool = True
    present_after: bool = True
    value_type_before: str | None = None
    value_type_after: str | None = None


def compare_app(
    before_rows: Sequence[NormalizedRow],
    after_rows: Sequence[NormalizedRow],
) -> list[ComparisonRow]:
    """Outer-join the two slices of one application on (PATH, ARRAY_KEY)."""
    index_b = {(r.path, r.array_key): r for r in before_rows}
    index_a = {(r.path, r.array_key): r for r in after_rows}
    keys = list(index_b.keys())
    keys.extend(k for k in index_a.keys() if k not in index_b)
    out: list[ComparisonRow] = []
    for path, array_key in keys:
        b = index_b.get((path, array_key), None)
        a = index_a.get((path, array_key), None)
        sample = b or a
        assert sample is not None
        if b is not None and a is not None:
            same = b.value == a.value and b.value_type == a.value_type
            ctype = SAME if same else VALUE_CHANGED
        elif b is None:
            ctype = ATTRIBUTE_NEW
        else:
            ctype = ATTRIBUTE_LOST
        out.append(
            ComparisonRow(
                app_id=sample.app_id,
                prefix=sample.prefix,
                cls=sample.cls,
                suffix=sample.suffix,
                array_key=array_key,
                key=sample.key,
                path=path,
                value_before=b.value if b else None,
                value_after=a.value if a else None,
                adjusted_before=b.adjusted_value if b else None,
                adjusted_after=a.adjusted_value if a else None,
                diff_flag=0 if ctype == SAME else 1,
                change_type=ctype,
                present_before=b is not None,
                present_after=a is not None,
                value_type_before=b.value_type if b else None,
                value_type_after=a.value_type if a else None,
            )
        )
    out.sort(key=lambda r: (r.path, r.array_key))
    return out


# ---------------------------------------------------------------------------
# bucketing (ADJUSTED_VALUE)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BucketRule:
    """``path_prefix`` -> numeric interval labels.

    Edges are *global* for the run, so BEFORE and AFTER always land in the same
    bucket for the same value.  ``DIFF_FLAG`` is never derived from the bucket.
    """

    path_prefix: str
    edges: tuple[float, ...]
    labels: tuple[str, ...] | None = None
    fmt: str = "{lo:g} - {hi:g}"

    def label_for(self, value: float) -> str:
        edges = self.edges
        labels = self.labels
        if labels is not None:
            for i in range(len(edges) - 1):
                lo, hi = edges[i], edges[i + 1]
                if lo < value <= hi:
                    return labels[i]
            return labels[-1] if value > edges[-1] else labels[0]
        n = len(edges) - 1
        for i in range(n):
            lo, hi = edges[i], edges[i + 1]
            if lo < value <= hi:
                return "(" + self.fmt.format(lo=lo, hi=hi) + "]"
        if value <= edges[0]:
            return f"<= {edges[0]:g}"
        return f"> {edges[-1]:g}"


def make_adjuster(rules: Sequence[BucketRule]) -> Callable[[str, str, str], str]:
    ordered = sorted(rules, key=lambda r: -len(r.path_prefix))

    def adjust(path: str, value: str, value_type: str) -> str:
        for rule in ordered:
            if path == rule.path_prefix or path.startswith(rule.path_prefix + "."):
                if value_type != "number":
                    return value
                try:
                    return rule.label_for(float(value))
                except ValueError:
                    return value
        return value

    return adjust
