"""FR-M1 / MAC-1, MAC-2: rule files are validated before any run."""
import copy
import json

import pytest

from ruleengine.rules import RuleValidationError, load_rule, load_rules_dir
from tests.ruleengine.conftest import RULES_DIR


def raw(name="TM-US-001"):
    return json.loads((RULES_DIR / f"{name}.json").read_text())


def test_shipped_rules_validate():
    rules = load_rules_dir(RULES_DIR)
    assert sorted(r.rule_id for r in rules) == ["TM-US-001", "TM-US-002", "TM-US-003"]


def test_mac1_unknown_field_fails_with_json_path():
    r = raw()
    r["filters"]["cashBelowThreshold"][0]["field"] = "transaction.foo"
    with pytest.raises(RuleValidationError) as e:
        load_rule(r)
    paths = [p for p, _ in e.value.errors]
    assert "$.filters.cashBelowThreshold[0].field" in paths


def test_mac1_operator_not_valid_for_field_type():
    r = raw()
    r["filters"]["cashBelowThreshold"][2]["operator"] = "CONTAINS"   # not an operator
    with pytest.raises(RuleValidationError):
        load_rule(r)
    r = raw()
    r["filters"]["cashBelowThreshold"][0]["operator"] = "LESS_THAN"  # boolean field
    with pytest.raises(RuleValidationError) as e:
        load_rule(r)
    assert any("not valid for" in m for _, m in e.value.errors)


def test_mac1_unknown_parameter_or_aggregation_reference():
    r = raw()
    r["match"]["all"][0]["right"] = {"param": "doesNotExist"}
    with pytest.raises(RuleValidationError) as e:
        load_rule(r)
    assert any("doesNotExist" in m for _, m in e.value.errors)
    r = raw()
    r["match"]["all"][0]["left"] = {"agg": "nope"}
    with pytest.raises(RuleValidationError):
        load_rule(r)


def test_mac2_duplicate_rule_version_with_different_content_fails(tmp_path):
    a = raw()
    b = copy.deepcopy(a)
    b["parameters"]["minCount"] = 4
    (tmp_path / "a.json").write_text(json.dumps(a))
    (tmp_path / "b.json").write_text(json.dumps(b))
    with pytest.raises(RuleValidationError) as e:
        load_rules_dir(tmp_path)
    assert any("duplicate" in m.lower() for _, m in e.value.errors)


def test_mac2_identical_duplicate_content_is_still_rejected(tmp_path):
    a = raw()
    (tmp_path / "a.json").write_text(json.dumps(a))
    (tmp_path / "b.json").write_text(json.dumps(a))
    with pytest.raises(RuleValidationError):
        load_rules_dir(tmp_path)
