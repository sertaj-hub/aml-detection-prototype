"""MAC-3..MAC-9: the three U.S. TM rules against hand-built fixtures, including boundaries."""
from decimal import Decimal

from ruleengine.evaluate import evaluate_rule
from tests.ruleengine.conftest import (D, atm, credit, dep, make_data, tx, wire_out)


def detect(rules, rule_id, txns, **kw):
    return evaluate_rule(make_data(txns, **kw), rules[rule_id], D)


# ---------------------------------------------------------------- TM-US-001
def test_mac3_structuring_three_cash_deposits_below_threshold_match(rules):
    d = detect(rules, "TM-US-001", [dep("t1", "A1", "2026-06-15 09:00", 3000), dep("t2", "A1", "2026-06-15 10:00", 3500),
                                    dep("t3", "A1", "2026-06-15 11:00", 4500)])
    assert len(d) == 1
    assert d[0]["metrics"]["cnt"] == 3 and d[0]["metrics"]["total"] == Decimal("11000.00")
    assert set(d[0]["trigger_txn_ids"]) == {"t1", "t2", "t3"}
    assert d[0]["severity"] == "HIGH" and d[0]["party_id"] == "P1"
    assert "3 cash transactions" in d[0]["explanation"] and "$11,000.00" in d[0]["explanation"]


def test_mac3_two_deposits_do_not_match(rules):
    assert detect(rules, "TM-US-001", [dep("t1", "A1", "2026-06-15 09:00", 4500), dep("t2", "A1", "2026-06-15 10:00", 4500)]) == []


def test_mac3_total_just_below_threshold_does_not_match(rules):
    t = [dep(f"t{i}", "A1", f"2026-06-15 {9 + i:02d}:00", 3333.33) for i in range(3)]   # 9,999.99
    assert detect(rules, "TM-US-001", t) == []


def test_mac3_total_exactly_at_threshold_matches(rules):
    t = [dep("t1", "A1", "2026-06-15 09:00", 3333.33), dep("t2", "A1", "2026-06-15 10:00", 3333.33),
         dep("t3", "A1", "2026-06-15 11:00", 3333.34)]
    assert len(detect(rules, "TM-US-001", t)) == 1


def test_mac3_deposit_of_exactly_threshold_does_not_qualify(rules):
    t = [dep("big", "A1", "2026-06-15 08:00", 10000), dep("t1", "A1", "2026-06-15 09:00", 3000),
         dep("t2", "A1", "2026-06-15 10:00", 3500), dep("t3", "A1", "2026-06-15 11:00", 4000)]
    d = detect(rules, "TM-US-001", t)
    assert len(d) == 1 and "big" not in d[0]["trigger_txn_ids"]


def test_mac4_window_start_is_exclusive(rules):
    t = [dep("t1", "A1", "2026-06-14 10:00", 4000), dep("t2", "A1", "2026-06-14 22:00", 4000),
         dep("t3", "A1", "2026-06-15 10:00", 4000)]          # t3 is exactly 24h after t1
    assert detect(rules, "TM-US-001", t) == []


def test_mac4_one_second_inside_window_matches(rules):
    t = [dep("t1", "A1", "2026-06-14 10:00", 4000), dep("t2", "A1", "2026-06-14 22:00", 4000),
         dep("t3", "A1", "2026-06-15 09:59:59", 4000)]
    d = detect(rules, "TM-US-001", t)
    assert len(d) == 1 and set(d[0]["trigger_txn_ids"]) == {"t1", "t2", "t3"}


def test_mac4_pattern_across_midnight_matches_on_the_later_date(rules):
    t = [dep("t1", "A1", "2026-06-14 22:00", 4000), dep("t2", "A1", "2026-06-14 23:30", 4000),
         dep("t3", "A1", "2026-06-15 01:00", 4000)]
    assert len(detect(rules, "TM-US-001", t)) == 1


def test_mac5_accounts_of_same_primary_party_are_combined(rules):
    t = [dep("t1", "A1", "2026-06-15 09:00", 4000), dep("t2", "A2", "2026-06-15 10:00", 4000),
         dep("t3", "A1", "2026-06-15 11:00", 4000)]
    d = detect(rules, "TM-US-001", t)
    assert len(d) == 1 and d[0]["entity_key"] == "P1"


def test_mac5_different_parties_are_not_combined(rules):
    t = [dep("t1", "A1", "2026-06-15 09:00", 4000), dep("t2", "A1", "2026-06-15 10:00", 4000),
         dep("t3", "A3", "2026-06-15 11:00", 4000)]
    assert detect(rules, "TM-US-001", t) == []


def test_mac5_cash_in_and_cash_out_are_not_combined(rules):
    t = [dep("t1", "A1", "2026-06-15 09:00", 4000), dep("t2", "A1", "2026-06-15 10:00", 4000),
         atm("t3", "A1", "2026-06-15 11:00", 4000)]
    assert detect(rules, "TM-US-001", t) == []


def test_mac5_three_cash_withdrawals_match_as_cash_out(rules):
    t = [atm(f"t{i}", "A1", f"2026-06-15 {9 + i:02d}:00", 4000) for i in range(3)]
    d = detect(rules, "TM-US-001", t)
    assert len(d) == 1 and d[0]["split_key"] == "DR"


def test_mac6_non_cash_never_qualifies(rules):
    t = [dep(f"t{i}", "A1", f"2026-06-15 {9 + i:02d}:00", 4000, is_cash=False) for i in range(3)]   # CASH_DEPOSIT, flag false
    t += [credit(f"c{i}", "A1", f"2026-06-15 {10 + i:02d}:00", 4000) for i in range(3)]
    assert detect(rules, "TM-US-001", t) == []


# ---------------------------------------------------------------- TM-US-002
def test_mac7_single_credit_above_multiple_and_floor_matches(rules):
    d = detect(rules, "TM-US-002", [credit("c1", "A1", "2026-06-15 10:00", 12000)])
    assert len(d) == 1 and d[0]["severity"] == "MEDIUM" and d[0]["party_id"] == "P1"


def test_mac7_credit_below_absolute_floor_does_not_match(rules):
    assert detect(rules, "TM-US-002", [credit("c1", "A1", "2026-06-15 10:00", 9999.99)]) == []


def test_mac7_single_credit_multiple_boundary(rules):
    ec = {"P1": 8000.0, "P2": 8000.0, "Q1": 4000.0}
    assert len(detect(rules, "TM-US-002", [credit("c1", "A1", "2026-06-15 10:00", 12000)], expected_credits=ec)) == 1
    assert detect(rules, "TM-US-002", [credit("c1", "A1", "2026-06-15 10:00", 11999.99)], expected_credits=ec) == []


def test_mac7_thirty_day_sum_boundary(rules):
    t = [credit("c1", "A1", "2026-06-01 10:00", 4000), credit("c2", "A2", "2026-06-08 10:00", 4000),
         credit("c3", "A1", "2026-06-15 10:00", 4000)]
    d = detect(rules, "TM-US-002", t)
    assert len(d) == 1 and d[0]["metrics"]["credits"] == Decimal("12000.00") and d[0]["metrics"]["creditCount"] == 3
    t[2] = credit("c3", "A1", "2026-06-15 10:00", 3999.99)
    assert detect(rules, "TM-US-002", t) == []


def test_mac7_expected_credits_are_floored_and_absolute_floor_applies(rules):
    ec = {"P1": 100.0, "P2": 4000.0, "Q1": 4000.0}       # floored to 500
    assert detect(rules, "TM-US-002", [credit("c1", "A1", "2026-06-15 10:00", 9000)], expected_credits=ec) == []


def test_mac7_internal_and_non_deposit_credits_do_not_count(rules):
    t = [credit("c1", "A1", "2026-06-15 10:00", 20000, channel="INTERNAL"),
         credit("c2", "A1", "2026-06-15 11:00", 20000, product_type="CARD")]
    assert detect(rules, "TM-US-002", t) == []


# ---------------------------------------------------------------- TM-US-003
def rapid(out_amount, counterparty="CP_EXT", n_credits=3, credit_amount=5000):
    t = [credit(f"c{i}", "A1", f"2026-06-14 {10 + 2 * i}:00", credit_amount) for i in range(n_credits)]
    t.append(wire_out("o1", "A1", "2026-06-15 09:00", out_amount, counterparty=counterparty))
    return t


def test_mac8_credits_followed_by_80_percent_outflow_match(rules):
    d = detect(rules, "TM-US-003", rapid(12000))
    assert len(d) == 1 and d[0]["metrics"]["outSum"] == Decimal("12000.00") and d[0]["entity_key"] == "A1"
    assert abs(d[0]["ratio"] - 1.0) < 1e-9


def test_mac8_outflow_just_below_ratio_does_not_match(rules):
    assert detect(rules, "TM-US-003", rapid(11999.99)) == []


def test_mac8_outflow_to_own_external_account_does_not_count(rules):
    assert detect(rules, "TM-US-003", rapid(12000, counterparty="CP_SELF")) == []


def test_mac8_two_credits_do_not_match(rules):
    assert detect(rules, "TM-US-003", rapid(9000, n_credits=2)) == []


def test_mac8_credits_older_than_48_hours_are_excluded(rules):
    t = [credit("old", "A1", "2026-06-12 10:00", 5000)] + rapid(12000, n_credits=2)[:2] + [wire_out("o1", "A1", "2026-06-15 09:00", 8000)]
    assert detect(rules, "TM-US-003", t) == []


# ---------------------------------------------------------------- nulls
def test_mac9_null_amount_or_cash_flag_never_matches(rules):
    t = [dep("t1", "A1", "2026-06-15 09:00", 4000), dep("t2", "A1", "2026-06-15 10:00", 4000),
         dep("t3", "A1", "2026-06-15 11:00", None)]
    assert detect(rules, "TM-US-001", t) == []
    t = [dep("t1", "A1", "2026-06-15 09:00", 4000), dep("t2", "A1", "2026-06-15 10:00", 4000),
         dep("t3", "A1", "2026-06-15 11:00", 4000, is_cash=None)]
    assert detect(rules, "TM-US-001", t) == []


def test_mac9_null_expected_credits_never_matches(rules):
    ec = {"P1": None, "P2": 4000.0, "Q1": 4000.0}
    assert detect(rules, "TM-US-002", [credit("c1", "A1", "2026-06-15 10:00", 20000)], expected_credits=ec) == []


# ---------------------------------------------------------------- proposed TM-US-002 v1.1 ("tipping credit" only)
def test_proposed_us002_v11_fires_only_on_the_credit_that_crosses_the_limit():
    from ruleengine.rules import load_rule
    from tests.ruleengine.conftest import ROOT
    rule = load_rule(ROOT / "ruleengine" / "rules_proposed" / "TM-US-002.json")
    t = [credit("c1", "A1", "2026-06-01 10:00", 4000), credit("c2", "A1", "2026-06-08 10:00", 4000),
         credit("c3", "A1", "2026-06-15 10:00", 4000)]                       # c3 takes the 30-day total to 12,000 (3x)
    assert len(evaluate_rule(make_data(t), rule, D)) == 1
    t.append(credit("c4", "A1", "2026-06-16 10:00", 500))                    # already above the limit: must not re-fire
    assert evaluate_rule(make_data(t), rule, __import__("datetime").date(2026, 6, 16)) == []
