"""The four change types and the NULL policy (PLAN.md §8.4, §9.2, §21.1).

«Различать эти четыре случая обязательно. „Правило перестало срабатывать“ и
„правило убрали из стратегии“ — разные события».
"""

from __future__ import annotations

import pytest

from change_analysis.pipeline.core import compare_app, normalize_document, serialize

pytestmark = pytest.mark.unit


def rows(document, source="BEFORE"):
    return normalize_document(document, source, "app-1")


def compare(before_doc, after_doc):
    return {r.path: r for r in compare_app(rows(before_doc), rows(after_doc, "AFTER"))}


def test_same():
    (row,) = compare({"a": {"x": 1}}, {"a": {"x": 1}}).values()
    assert row.change_type == "SAME" and row.diff_flag == 0
    assert row.value_before == row.value_after == "1"


def test_value_changed():
    (row,) = compare({"a": {"x": 1}}, {"a": {"x": 2}}).values()
    assert row.change_type == "VALUE_CHANGED" and row.diff_flag == 1


def test_attribute_new():
    (row,) = compare({"a": {}}, {"a": {"x": 2}}).values()
    assert row.change_type == "ATTRIBUTE_NEW" and row.diff_flag == 1
    assert row.value_before is None and row.value_after == "2"


def test_attribute_lost():
    (row,) = compare({"a": {"x": 2}}, {"a": {}}).values()
    assert row.change_type == "ATTRIBUTE_LOST" and row.diff_flag == 1
    assert row.value_before == "2" and row.value_after is None


def test_four_types_coexist_in_one_application():
    before = {"a": {"same": 1, "changed": 1, "lost": 1}}
    after = {"a": {"same": 1, "changed": 2, "new": 1}}
    result = compare(before, after)
    assert {path: row.change_type for path, row in result.items()} == {
        "a.same": "SAME",
        "a.changed": "VALUE_CHANGED",
        "a.lost": "ATTRIBUTE_LOST",
        "a.new": "ATTRIBUTE_NEW",
    }


# ---------------------------------------------------------------------------
# null / "NULL" / "" / absent — never confused (PLAN.md §21.1)
# ---------------------------------------------------------------------------


def test_null_becomes_false_is_a_value_change():
    """The agreed policy: JSON null → false is VALUE_CHANGED, not "the same"."""
    (row,) = compare({"a": {"hit": None}}, {"a": {"hit": False}}).values()
    assert row.change_type == "VALUE_CHANGED" and row.diff_flag == 1
    assert row.value_before == "NULL" and row.value_after == "false"


def test_four_null_like_states_are_distinct():
    """null, the string "NULL", the empty string and an absent attribute."""
    before = {"a": {"real_null": None, "text_null": "NULL", "empty": "", "kept": 1}}
    after = {"a": {"real_null": None, "text_null": "NULL", "empty": "", "kept": 1,
                   "absent_before": 1}}
    result = compare(before, after)

    assert result["a.real_null"].value_before == "NULL"
    assert result["a.text_null"].value_before == "NULL"
    # the text representation collides on purpose; the value_type column is what
    # separates a real null from the four-character string
    assert result["a.real_null"].value_type_before == "null"
    assert result["a.text_null"].value_type_before == "string"
    assert result["a.empty"].value_before == ""
    assert result["a.empty"].value_type_before == "string"
    assert result["a.absent_before"].change_type == "ATTRIBUTE_NEW"
    assert result["a.absent_before"].value_before is None


@pytest.mark.parametrize("value", [None, "NULL", ""])
def test_a_value_is_never_silently_equated_with_a_different_state(value):
    """A real null, the string "NULL" and "" must not compare equal to each other."""
    (row,) = compare({"a": {"x": value}}, {"a": {"x": False}}).values()
    assert row.change_type == "VALUE_CHANGED"


def test_bool_and_number_are_not_interchangeable():
    (row,) = compare({"a": {"x": True}}, {"a": {"x": 1}}).values()
    assert row.change_type == "VALUE_CHANGED"
    assert (row.value_before, row.value_type_before) == ("true", "boolean")
    assert (row.value_after, row.value_type_after) == ("1", "number")


def test_integral_float_equals_int():
    """710 and 710.0 must compare equal (PLAN.md §8.4)."""
    (row,) = compare({"a": {"x": 710}}, {"a": {"x": 710.0}}).values()
    assert row.change_type == "SAME"
    assert serialize(710.0) == serialize(710) == ("710", "number")


def test_string_digits_are_not_a_number():
    (row,) = compare({"a": {"x": "710"}}, {"a": {"x": 710}}).values()
    assert row.change_type == "VALUE_CHANGED"
    assert (row.value_type_before, row.value_type_after) == ("string", "number")


# ---------------------------------------------------------------------------
# ordering / stability
# ---------------------------------------------------------------------------


def test_rows_are_sorted_by_path_then_array_key():
    before = {"b": {"y": 1}, "a": {"x": 1}, "ext_data": [{"body": {"action": "Z"},
                                                          "v": 1},
                                                         {"body": {"action": "A"},
                                                          "v": 1}]}
    after = {"b": {"y": 2}, "a": {"x": 2}}
    compared = compare_app(rows(before), rows(after, "AFTER"))
    keys = [(r.path, r.array_key) for r in compared]
    assert keys == sorted(keys)


def test_same_rows_are_kept_for_every_attribute():
    """SAME rows are needed as denominators — dropping them breaks the report."""
    before = {"a": {"x": 1, "y": 2, "z": 3}}
    after = {"a": {"x": 1, "y": 2, "z": 3}}
    compared = compare_app(rows(before), rows(after, "AFTER"))
    assert len(compared) == 3
    assert not any(r.diff_flag for r in compared)
