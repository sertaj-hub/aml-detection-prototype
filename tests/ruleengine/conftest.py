"""Shared fixtures: tiny hand-built transaction sets in an in-memory DuckDB, shaped like the prototype data."""
from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

from ruleengine import store
from ruleengine.engine import Engine
from ruleengine.policy import load_policy
from ruleengine.rules import load_rules_dir

ROOT = Path(__file__).resolve().parents[2]
RULES_DIR = ROOT / "ruleengine" / "rules"
POLICY_PATH = ROOT / "ruleengine" / "policy" / "mvp_policy.json"
D = date(2026, 6, 15)

TXN_DEFAULTS = {
    "CASH_DEPOSIT": dict(channel="BRANCH", is_cash=True, direction="CR"),
    "ATM_WITHDRAWAL": dict(channel="ATM", is_cash=True, direction="DR"),
    "ACH_CREDIT": dict(channel="ACH", is_cash=False, direction="CR"),
    "WIRE_OUT": dict(channel="WIRE", is_cash=False, direction="DR"),
    "WIRE_IN": dict(channel="WIRE", is_cash=False, direction="CR"),
}


def tx(txn_id, account, when, txn_type, amount, **kw):
    base = dict(txn_id=txn_id, account_id=account, product_type="DEPOSIT", txn_ts=datetime.fromisoformat(when),
                txn_type=txn_type, direction="CR", amount=amount, channel="ACH", is_cash=False,
                counterparty_id=None, counter_account_id=None, cp_country="US", mcc=None,
                location_state="NY", initiated_by_party_id=None, description=None)
    base.update(TXN_DEFAULTS.get(txn_type, {}))
    base.update(kw)
    return base


def dep(txn_id, account, when, amount, **kw):
    return tx(txn_id, account, when, "CASH_DEPOSIT", amount, **kw)


def atm(txn_id, account, when, amount, **kw):
    return tx(txn_id, account, when, "ATM_WITHDRAWAL", amount, **kw)


def credit(txn_id, account, when, amount, **kw):
    return tx(txn_id, account, when, "ACH_CREDIT", amount, **kw)


def wire_out(txn_id, account, when, amount, counterparty="CP_EXT", **kw):
    return tx(txn_id, account, when, "WIRE_OUT", amount, counterparty_id=counterparty, **kw)


DEFAULT_ROLES = [("P1", "A1", "PRIMARY"), ("P1", "A2", "PRIMARY"), ("P2", "A3", "PRIMARY"), ("Q1", "A1", "JOINT")]
DEFAULT_PARTIES = {"P1": 4000.0, "P2": 4000.0, "Q1": 4000.0}
DEFAULT_CPS = [("CP_EXT", "INDIVIDUAL"), ("CP_SELF", "SELF_EXTERNAL")]


def make_data(txns, roles=None, expected_credits=None, cps=None):
    c = duckdb.connect()
    c.execute("""CREATE TABLE transactions(txn_id VARCHAR, account_id VARCHAR, product_type VARCHAR, txn_ts TIMESTAMP,
        txn_type VARCHAR, direction VARCHAR, amount DOUBLE, channel VARCHAR, is_cash BOOLEAN, counterparty_id VARCHAR,
        counter_account_id VARCHAR, cp_country VARCHAR, mcc VARCHAR, location_state VARCHAR,
        initiated_by_party_id VARCHAR, description VARCHAR)""")
    cols = ["txn_id", "account_id", "product_type", "txn_ts", "txn_type", "direction", "amount", "channel", "is_cash",
            "counterparty_id", "counter_account_id", "cp_country", "mcc", "location_state", "initiated_by_party_id",
            "description"]
    for t in txns:
        c.execute(f"INSERT INTO transactions VALUES ({','.join('?' * len(cols))})", [t[k] for k in cols])
    c.execute("CREATE TABLE party_account_role(party_id VARCHAR, account_id VARCHAR, role VARCHAR, start_date DATE, end_date INTEGER)")
    for p, a, r in (roles or DEFAULT_ROLES):
        c.execute("INSERT INTO party_account_role VALUES (?,?,?,NULL,NULL)", [p, a, r])
    c.execute("CREATE TABLE parties(party_id VARCHAR, expected_monthly_credits DOUBLE, expected_monthly_cash DOUBLE, "
              "pep_flag BOOLEAN, kyc_risk VARCHAR)")
    for p, e in (expected_credits or DEFAULT_PARTIES).items():
        c.execute("INSERT INTO parties VALUES (?,?,?,?,?)", [p, e, 500.0, False, "LOW"])
    c.execute("CREATE TABLE counterparties(counterparty_id VARCHAR, cp_name VARCHAR, cp_type VARCHAR, country VARCHAR, "
              "state VARCHAR, mcc VARCHAR, linked_party_id VARCHAR)")
    for cid, ctype in (cps or DEFAULT_CPS):
        c.execute("INSERT INTO counterparties VALUES (?,?,?,?,?,?,?)", [cid, cid, ctype, "US", "NY", None, None])
    return c


@pytest.fixture
def rules():
    return {r.rule_id: r for r in load_rules_dir(RULES_DIR)}


@pytest.fixture
def policy():
    return load_policy(POLICY_PATH)


@pytest.fixture
def make_engine(rules, policy):
    def _make(txns, only=None, policy_override=None, **data_kw):
        data = make_data(txns, **data_kw)
        sconn = store.connect(":memory:")
        store.init_schema(sconn)
        rs = [r for rid, r in rules.items() if only is None or rid in only]
        return Engine(data, rs, policy_override or policy, sconn)
    return _make


def count(sconn, table):
    return sconn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
