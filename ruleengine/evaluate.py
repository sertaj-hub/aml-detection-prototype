"""Evaluate one rule for one business date: compile, run, pick one detection per entity, rebuild evidence, reconcile."""
import re
from decimal import Decimal

from ruleengine import catalogue as C
from ruleengine import compiler
from ruleengine.ids import detection_id

SEVERITY_ORDER = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}


def money(v):
    return f"${Decimal(str(v)):,.2f}"


def fmt(value, spec):
    if value is None:
        return "n/a"
    if spec == "money":
        return money(value)
    if isinstance(value, Decimal):
        return str(int(value)) if value == value.to_integral_value() else str(value.normalize())
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def explain(template, ctx):
    return re.sub(r"\{(\w+)(?::(\w+))?\}", lambda m: fmt(ctx.get(m.group(1)), m.group(2)), template)


def reconcile(rule, metrics, trigger_rows):
    """Recompute every aggregation from the linked trigger transactions and compare with the window values."""
    errors = []
    for name, a in rule.raw["aggregations"].items():
        rows = [r for r in trigger_rows if r[f"in_{name}"]]
        if a["function"] == "COUNT":
            got = len(rows)
        else:
            vals = [Decimal(str(r["amount"])) for r in rows if r["amount"] is not None]
            got = {"SUM": lambda v: sum(v, Decimal("0.00")), "MAX": max, "MIN": min}[a["function"]](vals) if vals else None
        want = metrics[name]
        if (got is None) != (want is None) or (got is not None and Decimal(str(got)) != Decimal(str(want))):
            errors.append(f"{name}: window value {want} != linked transactions {got}")
    return errors


def evaluate_rule(conn, rule, business_date):
    """Return detection dicts (status CANDIDATE) for the rule on the business date. Raises on SQL errors."""
    conn.execute("CREATE OR REPLACE TEMP TABLE _pop AS " + compiler.pop_sql(rule, business_date))
    cur = conn.execute(compiler.match_sql_full(rule, business_date))
    cols = [d[0] for d in cur.description]
    matches = [dict(zip(cols, r)) for r in cur.fetchall()]
    if not matches:
        return []
    # one detection per entity/split: strongest ratio, latest anchor on ties
    best = {}
    for m in matches:
        k = (m["entity_key"], m["split_key"])
        if k not in best or (m["ratio"], m["txn_ts"]) > (best[k]["ratio"], best[k]["txn_ts"]):
            best[k] = m
    conn.execute("CREATE OR REPLACE TEMP TABLE _chosen(entity_key VARCHAR, split_key VARCHAR, anchor_ts TIMESTAMP)")
    for (e, s), m in best.items():
        conn.execute("INSERT INTO _chosen VALUES (?,?,?)", [e, s, m["txn_ts"]])
    cur = conn.execute(compiler.trigger_sql(rule))
    tcols = [d[0] for d in cur.description]
    by_key = {}
    for row in cur.fetchall():
        r = dict(zip(tcols, row))
        by_key.setdefault((r["c_entity"], r["c_split"]), []).append(r)

    raw, out = rule.raw, []
    agg_names = list(raw["aggregations"])
    for (e, s), m in sorted(best.items()):
        rows = by_key[(e, s)]
        trig = [r for r in rows if any(r[f"in_{n}"] for n in agg_names)]
        metrics = {n: m[f"agg_{n}"] for n in agg_names}
        errors = reconcile(rule, metrics, trig)
        ids = sorted(r["txn_id"] for r in trig)
        anchor = m["txn_ts"]
        ctx = {**rule.params, **metrics, "anchorAmount": m["amount"], "expected": m["exp_credits"], "expectedCash": m["exp_cash"]}
        out.append({
            "detection_id": detection_id(rule.rule_id, e, s, ids, anchor),
            "rule_id": rule.rule_id, "rule_version": rule.version, "rule_name": rule.name, "typology": raw.get("typology"),
            "regulation": raw["regulation"], "entity_key": e, "split_key": s, "party_id": m["party_id"],
            "business_date": business_date, "window_start": None, "window_end": anchor,
            "metrics": metrics, "ratio": float(m["ratio"]), "severity": raw["severity"], "points": None,
            "trigger_txn_ids": ids,
            "trigger_transactions": [{"txn_id": r["txn_id"], "account_id": r["account_id"], "txn_ts": r["txn_ts"].isoformat(),
                                      "txn_type": r["txn_type"], "direction": r["direction"], "amount": str(r["amount"]),
                                      "initiated_by_party_id": r["initiated_by_party_id"],
                                      "counts_toward": [n for n in agg_names if r[f"in_{n}"]]} for r in trig],
            "accounts": sorted({r["account_id"] for r in trig}),
            "explanation": explain(raw["explanation"], ctx),
            "reconciled": not errors, "reconciliation_errors": errors, "status": "CANDIDATE",
        })
        out[-1]["window_start"] = min(r["txn_ts"] for r in trig) if trig else anchor
    return out
