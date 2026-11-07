"""The serialization contract of CONTRACTS.md §0 and СХЕМА_ВИТРИНЫ §1.

These strings are what the delivered checks compare against
(``check_stop_factor_disappeared`` literally compares ``VALUE_BEFORE == "true"``),
so they are part of the public contract, not an implementation detail.
"""

from __future__ import annotations

import pytest

from change_analysis.pipeline.core import _split_path, escape_key, join_array_keys, serialize

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "value, expected",
    [
        (True, ("true", "boolean")),
        (False, ("false", "boolean")),
        (None, ("NULL", "null")),
        ("", ("", "string")),
        ("NULL", ("NULL", "string")),
        ("710", ("710", "string")),
        (710, ("710", "number")),
        (710.0, ("710", "number")),          # integral float collapses (PLAN.md §8.4)
        (0.55, ("0.55", "number")),
        (-1.5, ("-1.5", "number")),
    ],
)
def test_serialize_table(value, expected):
    assert serialize(value) == expected


def test_null_and_literal_NULL_are_distinguishable():
    """A real null and the four-character string must never look identical."""
    assert serialize(None) == ("NULL", "null")
    assert serialize("NULL") == ("NULL", "string")
    assert serialize(None)[1] != serialize("NULL")[1]


def test_serialize_rejects_containers():
    """Containers must be walked, never silently flattened into repr text."""
    with pytest.raises(TypeError):
        serialize({"a": 1})
    with pytest.raises(TypeError):
        serialize([1, 2])


@pytest.mark.parametrize(
    "path",
    [
        "characteristics.score_okb",
        "decisionResult.product_trials.rules.hit",
        "decisionResult.scoring.features.score_okb",
        "appData.request_id",
        "appData.customer.id",
    ],
)
def test_path_parts_reassemble_into_the_path(path):
    """PREFIX.CLASS.SUFFIX.KEY == PATH for every nested path (СХЕМА_ВИТРИНЫ §1)."""
    prefix, cls, suffix, key = _split_path(path)
    segments = [s for s in (prefix, cls, suffix) if s] + [key]
    assert ".".join(segments) == path
    assert key == path.rsplit(".", 1)[-1]
    assert prefix == path.split(".", 1)[0]


def test_single_segment_path_has_prefix_equal_to_key():
    """Documented corner: a top-level scalar has PREFIX == KEY, so the plain
    concatenation would duplicate it.  No delivered data has such a path, but the
    behaviour must not be a surprise to whoever reads the comment in core.py."""
    prefix, cls, suffix, key = _split_path("toplevel")
    assert (prefix, cls, suffix, key) == ("toplevel", "", "", "toplevel")


def test_array_key_component_escaping():
    """The ``|`` separator must not be confusable with data (CONTRACTS.md §0)."""
    assert escape_key("AGELOW@21") == "AGELOW@21"
    assert escape_key("A|B") == r"A\|B"
    assert join_array_keys(["15210", "AGELOW@21"]) == "15210|AGELOW@21"
    assert join_array_keys(["ML_MICRO_LOAN_V1", "0"]) == "ML_MICRO_LOAN_V1|0"
