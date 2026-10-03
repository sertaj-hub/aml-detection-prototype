"""MAC-10..MAC-14: detection identity, novelty, party-level alerts, evidence reconciliation."""
from datetime import date

import pytest

from ruleengine import evaluate, novelty, store
from ruleengine.evaluate import evaluate_rule
from ruleengine.ids import detection_id
from tests.ruleengine.conftest import D, count, credit, dep, make_data, wire_out


def structuring(prefix="t", account="A1", day="2026-06-15"):
    return [dep(f"{prefix}1", account, f"{day} 09:00", 3000), dep(f"{prefix}2", account, f"{day} 10:00", 3500),
            dep(f"{prefix}3", account, f"{day} 11:00", 4500)]


def test_mac10_detection_ids_are_deterministic(rules):
    a = evaluate_rule(make_data(structuring()), rules["TM-US-001"], D)
    b = evaluate_rule(make_data(structuring()), rules["TM-US-001"], D)
    assert a[0]["detection_id"] == b[0]["detection_id"]
    assert a[0]["detection_id"] == detection_id("TM-US-001", "P1", "CR", ["t1", "t2", "t3"], a[0]["window_end"])


def test_mac10_changing_one_trigger_transaction_changes_the_id(rules):
    a = evaluate_rule(make_data(structuring()), rules["TM-US-001"], D)
    t = structuring()
    t[2]["txn_id"] = "t9"
    b = evaluate_rule(make_data(t), rules["TM-US-001"], D)
    assert a[0]["detection_id"] != b[0]["detection_id"]


def test_mac11_novelty_classification():
    assert novelty.classify({"a", "b", "c"}, {"a", "b", "c", "d"}) == "SUPPRESSED_NO_NEW_EVIDENCE"
    assert novelty.classify({"a", "b", "c", "e"}, {"a", "b", "c"}) == "NEW_WITH_OVERLAP"
    assert novelty.classify({"x", "y"}, {"a", "b"}) == "NEW"
    assert novelty.classify({"x"}, set()) == "NEW"


def test_mac11_continuing_pattern_next_day_is_new_with_overlap(make_engine):
    t = structuring() + [dep("t4", "A1", "2026-06-16 08:00", 4000)]
    eng = make_engine(t, only=["TM-US-001"])
    r1 = eng.run_date(date(2026, 6, 15))
    r2 = eng.run_date(date(2026, 6, 16))
    assert [d["status"] for d in r1.detections] == ["NEW"]
    assert [d["status"] for d in r2.detections] == ["NEW_WITH_OVERLAP"]
    assert len(r2.alerts) == 1


def test_mac11_detection_fully_covered_by_earlier_detections_is_suppressed_and_forms_no_alert(make_engine, rules):
    t = structuring()
    eng = make_engine(t, only=["TM-US-001"])
    prior = evaluate_rule(eng.data, rules["TM-US-001"], D)[0]
    prior = dict(prior, detection_id="DET-prior", business_date=date(2026, 6, 14), status="NEW", run_id="R-old")
    store.record_detection(eng.store, prior)
    r = eng.run_date(D)
    assert [d["status"] for d in r.detections] == ["SUPPRESSED_NO_NEW_EVIDENCE"]
    assert r.alerts == [] and count(eng.store, "alert") == 0


def test_mac12_party_with_detections_from_two_rules_gets_one_alert(make_engine, policy):
    t = ([credit("c1", "A1", "2026-06-15 10:00", 12000)]                                   # TM-US-002 on P1
         + [credit(f"k{i}", "A2", f"2026-06-14 {10 + 2 * i}:00", 5000) for i in range(3)]      # TM-US-003 on A2 (P1)
         + [wire_out("o1", "A2", "2026-06-15 09:00", 12000)])
    probe = make_engine(t, only=["TM-US-002", "TM-US-003"])
    dets = probe.run_date(D, dry_run=True).detections
    assert sorted(d["rule_id"] for d in dets) == ["TM-US-002", "TM-US-003"]
    total = round(sum(d["points"] for d in dets), 1)

    at = dict(policy, dailyThreshold=total)
    eng = make_engine(t, only=["TM-US-002", "TM-US-003"], policy_override=at)
    r = eng.run_date(D)
    assert len(r.alerts) == 1 and r.alerts[0]["primary_party_id"] == "P1" and r.alerts[0]["score"] == total
    assert len(r.alerts[0]["detection_ids"]) == 2

    above = dict(policy, dailyThreshold=total + 0.1)
    eng = make_engine(t, only=["TM-US-002", "TM-US-003"], policy_override=above)
    assert eng.run_date(D).alerts == [] and count(eng.store, "alert") == 0


def test_mac12_points_follow_severity_policy_and_ratio_cap(make_engine):
    eng = make_engine(structuring(), only=["TM-US-001"])
    d = eng.run_date(D, dry_run=True).detections[0]
    assert d["ratio"] == pytest.approx(1.1) and d["points"] == round(40 * (1 + 0.25 * 0.1), 1)


def test_mac13_joint_account_alert_goes_to_primary_and_lists_joint_holder(make_engine):
    eng = make_engine(structuring(account="A1"), only=["TM-US-001"])
    r = eng.run_date(D)
    assert len(r.alerts) == 1 and r.alerts[0]["primary_party_id"] == "P1"
    rows = eng.store.execute("SELECT party_id, role FROM alert_party ORDER BY party_id").fetchall()
    assert ("Q1", "JOINT") in rows and ("P1", "PRIMARY") in rows
    assert eng.store.execute("SELECT COUNT(*) FROM alert WHERE primary_party_id='Q1'").fetchone()[0] == 0


def test_mac13_initiating_secondary_party_is_marked(make_engine):
    t = structuring()
    for x in t:
        x["initiated_by_party_id"] = "Q1"
    eng = make_engine(t, only=["TM-US-001"])
    eng.run_date(D)
    assert eng.store.execute("SELECT is_initiator FROM alert_party WHERE party_id='Q1'").fetchone()[0] == 1


def test_mac14_evidence_reconciles_with_linked_transactions(rules):
    d = evaluate_rule(make_data(structuring()), rules["TM-US-001"], D)[0]
    assert d["reconciled"] is True and d["reconciliation_errors"] == []
    assert sum(float(x["amount"]) for x in d["trigger_transactions"]) == pytest.approx(11000)


def test_mac14_inconsistent_evidence_creates_no_alert_and_records_failure(make_engine, monkeypatch):
    eng = make_engine(structuring(), only=["TM-US-001"])
    monkeypatch.setattr(evaluate, "reconcile", lambda *a, **k: ["total: window 11000.00 != linked 10999.00"])
    r = eng.run_date(D)
    assert r.alerts == [] and r.detections == [] and count(eng.store, "alert") == 0
    row = eng.store.execute("SELECT status, error FROM rule_execution WHERE rule_id='TM-US-001'").fetchone()
    assert row[0] == "RECONCILIATION_FAILED" and "window 11000.00" in row[1]
    assert r.status == "PARTIAL"
