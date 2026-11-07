"""Input readers are procedure code; discovery is format-independent."""
import json
from xml.etree import ElementTree as ET

import pytest

from change_analysis.procedures import spr_normalize as normalizer
from change_analysis.procedures.spr_normalize_xml import element_to_value, read_document
from change_analysis.services.datasets import discover_files, precheck_dataset
from change_analysis.services.errors import InvalidRequest
from change_analysis.config import Settings
from change_analysis.worker.runner import PipelineJob
from qa_support import FakeContext

pytestmark = pytest.mark.unit


def test_xml_rules():
    root = ET.fromstring('''<response code="7">
      <item enabled="true">12.5</item><item>false</item>
      <empty>   </empty><nested><n>1e2</n><nil>null</nil></nested>
    </response>''')
    assert element_to_value(root) == {
        "@code": 7, "item": [{"@enabled": True, "#text": 12.5}, False],
        "empty": "", "nested": {"n": 100.0, "nil": None}}


def test_xml_explicit_types():
    root = ET.fromstring('''<r xmlns:ca="urn:change-analysis:types">
      <a ca:type="array"><item ca:type="string">123</item></a>
      <b ca:type="array"/><c ca:type="null"/><d ca:type="object"/>
    </r>''')
    assert element_to_value(root) == {"a": ["123"], "b": [], "c": None, "d": {}}


def test_mixed_content_rejected():
    with pytest.raises(ValueError, match="mixed content"):
        element_to_value(ET.fromstring("<r>lost<child>value</child></r>"))


def test_xml_glob_precheck_and_worker(tmp_path):
    for side in ("before", "after"):
        (tmp_path / side).mkdir()
        (tmp_path / side / "app-1.xml").write_text("<response><n>1</n></response>")
    slices = {"BEFORE": {"dir": "before"}, "AFTER": {"dir": "after"}}
    (tmp_path / "manifest.json").write_text(json.dumps({"srez": slices, "applications": 1}))
    assert precheck_dataset(".", str(tmp_path), "*.xml")["pairs"] == 1
    assert precheck_dataset(".", str(tmp_path), "*.xml")["ok"]
    assert not precheck_dataset(".", str(tmp_path))["ok"]
    settings = Settings("unused", tmp_path, tmp_path, tmp_path / "art", "python")
    job = PipelineJob({"id": 1, "run_id": "test"}, settings)
    assert job._apps({"slices": slices, "file_glob": "*.xml"}) == [
        {"app_id": "app-1", "before": str(tmp_path / "before/app-1.xml"),
         "after": str(tmp_path / "after/app-1.xml")}]


def test_multiple_globs_and_duplicate_identity(tmp_path):
    (tmp_path / "one.json").touch()
    (tmp_path / "two.xml").touch()
    assert set(discover_files(tmp_path, "*.json,*.xml")) == {"one", "two"}
    (tmp_path / "one.xml").touch()
    with pytest.raises(InvalidRequest, match="duplicate APP_ID"):
        discover_files(tmp_path, "*.json;*.xml")
    with pytest.raises(InvalidRequest, match="file_glob"):
        discover_files(tmp_path, "../*.xml")


def test_unknown_extension(tmp_path):
    path = tmp_path / "app.weird"
    path.write_text("arbitrary")
    with pytest.raises(ValueError, match="Unknown input format.*custom .py reader"):
        normalizer.read_document(path)


def test_custom_reader(tmp_path):
    path = tmp_path / "app.anything"
    path.write_text("42")
    assert normalizer.read_document(path, {
        "reader_code": "def read_document(path):\n    return {'answer': int(path.read_text())}\n"
    }) == {"answer": 42}


def test_json_xml_normalized_rows_equal(tmp_path):
    document = {"appData": {"application_id": "app-1"},
                "characteristics": {"score_okb": 700, "flag": True},
                "decisionResult": {"decision": "APPROVE"}}
    json_path = tmp_path / "app-1.json"
    xml_path = tmp_path / "app-1.xml"
    json_path.write_text(json.dumps(document))
    xml_path.write_text('''<response><appData><application_id>app-1</application_id></appData>
      <characteristics><score_okb>700</score_okb><flag>true</flag></characteristics>
      <decisionResult><decision>APPROVE</decision></decisionResult></response>''')
    assert read_document(xml_path) == document
    def rows(path):
        result = normalizer.run(FakeContext(tmp_path / "art"), {"apps": [
            {"app_id": "app-1", "before": str(path), "after": str(path)}]}, {})
        values = list(result.rows)
        assert not result.diagnostics
        return values
    assert rows(json_path) == rows(xml_path)


def test_invalid_xml_is_readable_error(tmp_path):
    path = tmp_path / "bad.xml"
    path.write_text("<broken>")
    with pytest.raises(ValueError, match="Invalid XML"):
        read_document(path)
