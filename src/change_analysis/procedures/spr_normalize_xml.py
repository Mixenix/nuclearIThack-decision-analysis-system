"""XML reader owned by the normalization procedure, never by the kernel.

The document root is an envelope (its name is omitted). Child names become keys;
repeated siblings become lists. Attributes become @name keys. Leaf text becomes
a scalar, or #text when attributes coexist; whitespace-only nodes are ignored.
JSON numeric/boolean/null literals are decoded, other text stays a string.
Mixed content is rejected rather than silently discarded.

Optional {urn:change-analysis:types}type preserves JSON ambiguities on export:
string (literal text), null, object, array (children named item, including zero
or one child). This metadata attribute is consumed, not exposed as @type.
Without metadata an empty leaf is an empty string. Custom XML dialects can use
their own read_document(path) procedure instead.
"""

import json
from pathlib import Path
from xml.etree import ElementTree as ET

TYPE = "{urn:change-analysis:types}type"


def _local(name: str) -> str:
    """Element/attribute name without the namespace: ``{urn:x}age`` -> ``age``.

    ElementTree renders namespaced names in Clark notation.  Left as is, the
    namespace would leak into ``PATH`` and two slices that declare the same
    namespace under different prefixes would compare as different attributes.
    Stripping keeps paths readable and comparable; the price is that two
    elements with the same local name in *different* namespaces merge into one
    key, which is documented in ``DECISIONS.md``.
    """
    if name.startswith("{"):
        return name.split("}", 1)[1]
    return name


def _literal(text):
    try:
        value = json.loads(text)
        if value is None or isinstance(value, (bool, int, float)):
            return value
    except (ValueError, TypeError):
        pass
    return text


def element_to_value(element):
    # Only the namespaced marker is special: a plain ``type="loan"`` attribute is
    # ordinary data (README: "обычные атрибуты становятся @имя").  Matching by
    # local name here swallowed that attribute and, for ``type="null"``, threw
    # away the element's own content.
    kind = element.attrib.get(TYPE)
    children = list(element)
    text = element.text or ""
    if children and (text.strip() or any((c.tail or "").strip() for c in children)):
        raise ValueError("XML mixed content is unsupported; use a custom reader")
    if kind == "null":
        return None
    if kind == "string":
        return text
    if kind == "array":
        return [element_to_value(c) for c in children]
    attrs = {"@" + _local(k): _literal(v)
             for k, v in element.attrib.items() if k != TYPE}
    if not children and not attrs and kind != "object":
        return _literal(text.strip()) if text.strip() else ""
    result = dict(attrs)
    grouped = {}
    for child in children:
        grouped.setdefault(_local(child.tag), []).append(element_to_value(child))
    for key, values in grouped.items():
        result[key] = values if len(values) > 1 else values[0]
    if text.strip():
        result["#text"] = _literal(text.strip())
    return result


def read_document(path: Path) -> dict:
    try:
        value = element_to_value(ET.fromstring(path.read_bytes()))
    except ET.ParseError as exc:
        raise ValueError(f"Invalid XML: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("XML response root must contain an object")
    return value


def run(context, inputs, params):
    from change_analysis.procedures.spr_normalize import run as normalize
    return normalize(context, inputs, {**(params or {}), "input_format": "xml"})


PROCEDURE = {"id": "spr_normalize_xml_v1", "label": "СПР: XML",
             "stage": "normalize", "contract_version": 1, "params": {},
             "entrypoint": run}
