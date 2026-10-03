"""Compile a rule (JSON) to SQL (FR-M3). No row loops: windows are SQL window functions.

Pipeline of CTEs:  base (join + enrichment) -> pop (entity/split keys + named-filter flags, rows used by the rule)
                   -> win (window aggregations per row) -> matched (anchor rows of the business date that satisfy the rule).
Windows are trailing and start-exclusive, (t - N, t], implemented as RANGE [t - N + 1 microsecond, t].
Amounts are exact DECIMAL(18,2). NULL never matches (SQL three-valued logic, flags wrapped in COALESCE(..., FALSE)).
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from ruleengine import catalogue as C


def lit(v):
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float, Decimal)):
        return f"CAST('{Decimal(str(v))}' AS DECIMAL(18,4))"
    return "'" + str(v).replace("'", "''") + "'"


def resolve(v, params):
    return params[v["param"]] if isinstance(v, dict) else v


def window_us(w, params):
    return int(Decimal(str(resolve(w["size"], params))) * C.WINDOW_UNITS[w["unit"]])


def col(field, alias):
    return f"{alias}.{C.FIELDS[field][1]}"


def cond_sql(c, params, alias="b"):
    x, op = col(c["field"], alias), c["operator"]
    if op in ("IS_NULL", "IS_NOT_NULL"):
        return f"({x} IS {'NOT ' if op == 'IS_NOT_NULL' else ''}NULL)"
    v = c["value"]
    if op in ("IN", "NOT_IN"):
        return f"({x} {'NOT ' if op == 'NOT_IN' else ''}IN ({', '.join(lit(resolve(i, params)) for i in v)}))"
    if op == "BETWEEN":
        return f"({x} BETWEEN {lit(resolve(v[0], params))} AND {lit(resolve(v[1], params))})"
    return f"({x} {C.COMPARISONS[op]} {lit(resolve(v, params))})"


def filter_sql(conds, params, alias="b"):
    return "COALESCE(" + " AND ".join(cond_sql(c, params, alias) for c in conds) + ", FALSE)"


def operand_sql(o, params, alias="w"):
    if isinstance(o, (int, float)):
        return lit(o)
    k = next(iter(o))
    if k == "param":
        return lit(params[o["param"]])
    if k == "agg":
        return f"{alias}.agg_{o['agg']}"
    if k == "field":
        return col(o["field"], alias)
    if k == "profile":
        c = col(o["profile"], alias)
        floor = operand_sql(o["floor"], params, alias) if "floor" in o else "0"
        return f"(CASE WHEN {c} IS NULL THEN NULL ELSE GREATEST({c}, {floor}) END)"
    if k == "mul":
        return "(" + " * ".join(operand_sql(x, params, alias) for x in o["mul"]) + ")"
    if k == "sub":
        a, b = (operand_sql(x, params, alias) for x in o["sub"])
        return f"({a} - {b})"
    if k == "div":
        a, b = (operand_sql(x, params, alias) for x in o["div"])
        return f"(CAST({a} AS DOUBLE) / NULLIF(CAST({b} AS DOUBLE), 0))"
    if k == "max":
        return "GREATEST(" + ", ".join(operand_sql(x, params, alias) for x in o["max"]) + ")"
    raise ValueError(o)


def match_sql(m, params):
    if "all" in m:
        return "(" + " AND ".join(match_sql(x, params) for x in m["all"]) + ")"
    if "any" in m:
        return "(" + " OR ".join(match_sql(x, params) for x in m["any"]) + ")"
    return f"({operand_sql(m['left'], params)} {C.COMPARISONS[m['operator']]} {operand_sql(m['right'], params)})"


def max_window_us(raw):
    return max(window_us(a["window"], raw.get("parameters", {})) for a in raw["aggregations"].values())


def pop_sql(rule, business_date):
    """SQL producing the rule's working set: rows in [D - max window, D + 1) that matter to the rule, with flags."""
    raw, params = rule.raw, rule.params
    lo = datetime.combine(business_date, datetime.min.time()) - timedelta(microseconds=max_window_us(raw))
    hi = datetime.combine(business_date + timedelta(days=1), datetime.min.time())
    entity = f"b.{C.ENTITY_KEYS[raw['entity']['groupBy']]}"
    split = raw["entity"].get("splitBy", [])
    split_expr = " || '|' || ".join(f"b.{C.SPLIT_FIELDS[s]}" for s in split) if split else "''"
    flags = ",\n    ".join(f"{filter_sql(conds, params)} AS f_{name}" for name, conds in raw["filters"].items())
    used = sorted({a["filter"] for a in raw["aggregations"].values()} | {raw["anchor"]["filter"]})
    where = " OR ".join(f"f_{u}" for u in used)
    return f"""
WITH base AS (
  SELECT t.txn_id, t.account_id, t.product_type, t.txn_ts, t.txn_type, t.direction,
         CAST(t.amount AS DECIMAL(18,2)) AS amount, t.channel, t.is_cash, t.cp_country, c.cp_type,
         CASE WHEN c.cp_type = 'SELF_EXTERNAL' OR t.counter_account_id IS NOT NULL
                   OR t.txn_type IN ('TRANSFER_IN', 'TRANSFER_OUT') THEN 'OWN' ELSE 'EXTERNAL' END AS cp_relation,
         t.initiated_by_party_id, r.party_id AS party_id,
         CAST(p.expected_monthly_credits AS DECIMAL(18,2)) AS exp_credits,
         CAST(p.expected_monthly_cash AS DECIMAL(18,2)) AS exp_cash, p.pep_flag, p.kyc_risk
  FROM transactions t
  JOIN party_account_role r ON r.account_id = t.account_id AND r.role = 'PRIMARY'
  LEFT JOIN counterparties c ON c.counterparty_id = t.counterparty_id
  LEFT JOIN parties p ON p.party_id = r.party_id
  WHERE t.txn_ts >= TIMESTAMP '{lo.isoformat(sep=' ')}' AND t.txn_ts < TIMESTAMP '{hi.isoformat(sep=' ')}'
),
flagged AS (
  SELECT b.*, {entity} AS entity_key, {split_expr} AS split_key,
    {flags}
  FROM base b
)
SELECT * FROM flagged WHERE {where}"""


def match_sql_full(rule, business_date, pop_table="_pop"):
    """SQL over the materialised working set: anchors of the business date that satisfy the rule."""
    raw, params = rule.raw, rule.params
    aggs = []
    for name, a in raw["aggregations"].items():
        n = window_us(a["window"], params) - 1
        frame = (f"OVER (PARTITION BY entity_key, split_key ORDER BY txn_ts "
                 f"RANGE BETWEEN INTERVAL '{n} microseconds' PRECEDING AND CURRENT ROW)")
        f = f"f_{a['filter']}"
        if a["function"] == "COUNT":
            aggs.append(f"COUNT(CASE WHEN {f} THEN 1 END) {frame} AS agg_{name}")
        elif a["function"] == "SUM":
            aggs.append(f"COALESCE(SUM(CASE WHEN {f} THEN {col(a['field'], 'p')} END) {frame}, 0) AS agg_{name}")
        else:
            aggs.append(f"{a['function']}(CASE WHEN {f} THEN {col(a['field'], 'p')} END) {frame} AS agg_{name}")
    d0 = datetime.combine(business_date, datetime.min.time())
    d1 = d0 + timedelta(days=1)
    return f"""
WITH win AS (
  SELECT p.*, {', '.join(aggs)}
  FROM {pop_table} p
),
matched AS (
  SELECT w.*, ({match_sql(raw['match'], params)}) AS is_match, {operand_sql(raw['severityRatio'], params)} AS ratio
  FROM win w
  WHERE w.f_{raw['anchor']['filter']}
    AND w.txn_ts >= TIMESTAMP '{d0.isoformat(sep=' ')}' AND w.txn_ts < TIMESTAMP '{d1.isoformat(sep=' ')}'
)
SELECT * FROM matched WHERE COALESCE(is_match, FALSE)"""


def trigger_sql(rule, pop_table="_pop", chosen_table="_chosen"):
    """Independent recomputation path: the transactions inside each chosen anchor's windows, flagged per aggregation."""
    raw, params = rule.raw, rule.params
    flags = []
    for name, a in raw["aggregations"].items():
        flags.append(f"(p.f_{a['filter']} AND p.txn_ts > c.anchor_ts - INTERVAL '{window_us(a['window'], params)} microseconds') AS in_{name}")
    return f"""
SELECT c.entity_key AS c_entity, c.split_key AS c_split, c.anchor_ts,
       p.txn_id, p.account_id, p.txn_ts, p.txn_type, p.direction, p.amount, p.initiated_by_party_id, {', '.join(flags)}
FROM {chosen_table} c
JOIN {pop_table} p ON p.entity_key = c.entity_key AND p.split_key = c.split_key
 AND p.txn_ts <= c.anchor_ts AND p.txn_ts > c.anchor_ts - INTERVAL '{max_window_us(raw)} microseconds'
ORDER BY c.entity_key, c.split_key, p.txn_ts, p.txn_id"""
