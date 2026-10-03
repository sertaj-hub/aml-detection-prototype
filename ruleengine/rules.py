"""Rule definition language v0: loading and validation (FR-M1, FR-M2).

Two stages: JSON Schema (structure), then semantic checks (catalogue fields, operators vs type, references).
Errors carry a JSON path such as ``$.filters.cashBelowThreshold[0].field``.
"""
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import jsonschema

from ruleengine import catalogue as C


class RuleValidationError(Exception):
    def __init__(self, errors):
        self.errors = errors                      # list of (json_path, message)
        super().__init__("; ".join(f"{p}: {m}" for p, m in errors))


NUM_OR_PARAM = {"oneOf": [{"type": "number"}, {"type": "object", "required": ["param"],
                                                "properties": {"param": {"type": "string"}}, "additionalProperties": False}]}
OPERAND = {"$ref": "#/$defs/operand"}
SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["ruleId", "version", "name", "status", "cadence", "evaluate", "severity", "regulation", "entity",
                 "filters", "anchor", "aggregations", "match", "severityRatio", "explanation"],
    "additionalProperties": False,
    "properties": {
        "ruleId": {"type": "string", "pattern": r"^[A-Z0-9-]+$"},
        "version": {"type": "string"},
        "name": {"type": "string"},
        "status": {"enum": ["DRAFT", "VALIDATED", "TESTED", "APPROVED", "ACTIVE", "SUSPENDED", "RETIRED"]},
        "cadence": {"enum": ["DAILY", "MONTHLY"]},
        "evaluate": {"enum": ["EVENT_TIME", "SNAPSHOT"]},
        "typology": {"type": "string"},
        "severity": {"enum": C.SEVERITIES},
        "regulation": {"type": "object", "required": ["framework"], "additionalProperties": False,
                       "properties": {"framework": {"type": "string"}, "references": {"type": "array", "items": {"type": "string"}},
                                      "note": {"type": "string"}}},
        "parameters": {"type": "object", "additionalProperties": {"type": "number"}},
        "entity": {"type": "object", "required": ["groupBy"], "additionalProperties": False,
                   "properties": {"groupBy": {"enum": list(C.ENTITY_KEYS)},
                                  "splitBy": {"type": "array", "items": {"enum": list(C.SPLIT_FIELDS)}}}},
        "filters": {"type": "object", "additionalProperties": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/condition"}}},
        "anchor": {"type": "object", "required": ["filter"], "additionalProperties": False, "properties": {"filter": {"type": "string"}}},
        "aggregations": {"type": "object", "minProperties": 1, "additionalProperties": {
            "type": "object", "required": ["function", "filter", "window"], "additionalProperties": False,
            "properties": {"function": {"enum": C.AGG_FUNCTIONS}, "field": {"type": "string"}, "filter": {"type": "string"},
                           "window": {"type": "object", "required": ["size", "unit"], "additionalProperties": False,
                                      "properties": {"size": NUM_OR_PARAM, "unit": {"enum": list(C.WINDOW_UNITS)}}}}}},
        "match": {"$ref": "#/$defs/match"},
        "severityRatio": OPERAND,
        "explanation": {"type": "string"},
    },
    "$defs": {
        "condition": {"type": "object", "required": ["field", "operator"], "additionalProperties": False,
                      "properties": {"field": {"type": "string"}, "operator": {"enum": C.ALL_OPERATORS},
                                     "value": {}}},
        "operand": {"oneOf": [
            {"type": "number"},
            {"type": "object", "required": ["param"], "additionalProperties": False, "properties": {"param": {"type": "string"}}},
            {"type": "object", "required": ["agg"], "additionalProperties": False, "properties": {"agg": {"type": "string"}}},
            {"type": "object", "required": ["field"], "additionalProperties": False, "properties": {"field": {"type": "string"}}},
            {"type": "object", "required": ["profile"], "additionalProperties": False,
             "properties": {"profile": {"type": "string"}, "floor": {"$ref": "#/$defs/operand"}}},
            {"type": "object", "required": ["mul"], "additionalProperties": False,
             "properties": {"mul": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"$ref": "#/$defs/operand"}}}},
            {"type": "object", "required": ["div"], "additionalProperties": False,
             "properties": {"div": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"$ref": "#/$defs/operand"}}}},
            {"type": "object", "required": ["sub"], "additionalProperties": False,
             "properties": {"sub": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"$ref": "#/$defs/operand"}}}},
            {"type": "object", "required": ["max"], "additionalProperties": False,
             "properties": {"max": {"type": "array", "minItems": 2, "items": {"$ref": "#/$defs/operand"}}}},
        ]},
        "match": {"oneOf": [
            {"type": "object", "required": ["all"], "additionalProperties": False,
             "properties": {"all": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/match"}}}},
            {"type": "object", "required": ["any"], "additionalProperties": False,
             "properties": {"any": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/match"}}}},
            {"type": "object", "required": ["left", "operator", "right"], "additionalProperties": False,
             "properties": {"left": {"$ref": "#/$defs/operand"}, "operator": {"enum": sorted(C.COMPARISONS)},
                            "right": {"$ref": "#/$defs/operand"}}},
        ]},
    },
}
_VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)


def _path(parts):
    out = "$"
    for p in parts:
        out += f"[{p}]" if isinstance(p, int) else f".{p}"
    return out


@dataclass
class Rule:
    raw: dict
    sha256: str

    @property
    def rule_id(self): return self.raw["ruleId"]
    @property
    def version(self): return self.raw["version"]
    @property
    def name(self): return self.raw["name"]
    @property
    def status(self): return self.raw["status"]
    @property
    def cadence(self): return self.raw["cadence"]
    @property
    def severity(self): return self.raw["severity"]
    @property
    def params(self): return self.raw.get("parameters", {})


def canonical_hash(raw):
    return hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_rule(source):
    """source: dict or path to a JSON file. Returns a validated Rule or raises RuleValidationError."""
    if isinstance(source, (str, Path)):
        raw = json.loads(Path(source).read_text())
    else:
        raw = source
    errors = [(_path(e.absolute_path), e.message) for e in sorted(_VALIDATOR.iter_errors(raw), key=lambda e: list(map(str, e.absolute_path)))]
    if not errors:
        errors = _semantic(raw)
    if errors:
        raise RuleValidationError(errors)
    return Rule(raw=raw, sha256=canonical_hash(raw))


def load_rules_dir(directory):
    rules, seen, errors = [], {}, []
    for f in sorted(Path(directory).glob("*.json")):
        rule = load_rule(f)
        key = (rule.rule_id, rule.version)
        if key in seen:
            errors.append(("$", f"duplicate rule id and version {key[0]} v{key[1]} in {seen[key]} and {f.name}"))
        seen[key] = f.name
        rules.append(rule)
    if errors:
        raise RuleValidationError(errors)
    return rules


# --------------------------------------------------------------------------- semantic checks
def _semantic(r):
    errs = []
    params = r.get("parameters", {})
    filters, aggs = r["filters"], r["aggregations"]

    def check_value_ref(v, path):
        if isinstance(v, dict):
            if "param" not in v or v["param"] not in params:
                errs.append((path, f"unknown parameter '{v.get('param')}'"))

    def check_condition(c, path):
        spec = C.FIELDS.get(c["field"])
        if spec is None:
            errs.append((f"{path}.field", f"unknown field '{c['field']}' (not in the field catalogue)"))
            return
        ftype = spec[0]
        if c["operator"] not in C.OPERATORS[ftype]:
            errs.append((f"{path}.operator", f"operator {c['operator']} is not valid for {ftype} field {c['field']}"))
            return
        op = c["operator"]
        if op in ("IS_NULL", "IS_NOT_NULL"):
            return
        if "value" not in c:
            errs.append((f"{path}.value", "value is required"))
            return
        v = c["value"]
        if op in ("IN", "NOT_IN"):
            if not isinstance(v, list) or not v:
                errs.append((f"{path}.value", f"{op} needs a non-empty list"))
        elif op == "BETWEEN":
            if not (isinstance(v, list) and len(v) == 2):
                errs.append((f"{path}.value", "BETWEEN needs a list of two values"))
        else:
            check_value_ref(v, f"{path}.value")
            if ftype == "boolean" and not isinstance(v, bool):
                errs.append((f"{path}.value", "boolean field needs true or false"))

    def check_operand(o, path):
        if isinstance(o, (int, float)):
            return
        k = next(iter(o))
        if k == "param":
            check_value_ref(o, path)
        elif k == "agg":
            if o["agg"] not in aggs:
                errs.append((path, f"unknown aggregation '{o['agg']}'"))
        elif k == "field":
            if C.FIELDS.get(o["field"], (None,))[0] != "numeric":
                errs.append((path, f"field '{o['field']}' is not a numeric catalogue field"))
        elif k == "profile":
            if not o["profile"].startswith("party.") or C.FIELDS.get(o["profile"], (None,))[0] != "numeric":
                errs.append((path, f"'{o['profile']}' is not a numeric party profile field"))
            if "floor" in o:
                check_operand(o["floor"], f"{path}.floor")
        else:
            for i, x in enumerate(o[k]):
                check_operand(x, f"{path}.{k}[{i}]")

    def check_match(m, path):
        if "all" in m or "any" in m:
            key = "all" if "all" in m else "any"
            for i, x in enumerate(m[key]):
                check_match(x, f"{path}.{key}[{i}]")
        else:
            check_operand(m["left"], f"{path}.left")
            check_operand(m["right"], f"{path}.right")

    if r["evaluate"] != "EVENT_TIME":
        errs.append(("$.evaluate", "only EVENT_TIME is supported in the MVP"))
    for name, conds in filters.items():
        for i, c in enumerate(conds):
            check_condition(c, f"$.filters.{name}[{i}]")
            if isinstance(c.get("value"), dict):
                check_value_ref(c["value"], f"$.filters.{name}[{i}].value")
    if r["anchor"]["filter"] not in filters:
        errs.append(("$.anchor.filter", f"unknown filter '{r['anchor']['filter']}'"))
    for name, a in aggs.items():
        p = f"$.aggregations.{name}"
        if a["filter"] not in filters:
            errs.append((f"{p}.filter", f"unknown filter '{a['filter']}'"))
        if a["function"] != "COUNT":
            if "field" not in a:
                errs.append((f"{p}.field", f"{a['function']} needs a field"))
            elif C.FIELDS.get(a["field"], (None,))[0] != "numeric":
                errs.append((f"{p}.field", f"'{a['field']}' is not a numeric catalogue field"))
        size = a["window"]["size"]
        if isinstance(size, dict):
            check_value_ref(size, f"{p}.window.size")
    check_match(r["match"], "$.match")
    check_operand(r["severityRatio"], "$.severityRatio")
    known = set(params) | set(aggs) | C.EXPLANATION_EXTRAS
    for ph in re.findall(r"\{(\w+)(?::\w+)?\}", r["explanation"]):
        if ph not in known:
            errs.append(("$.explanation", f"unknown placeholder '{{{ph}}}'"))
    return errs
