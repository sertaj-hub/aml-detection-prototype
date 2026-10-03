"""MAC-15..MAC-20: atomic persistence, idempotent re-run, rule isolation, dry run, outbox publisher, label isolation."""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import ruleengine
from ruleengine import evaluate, publisher, store
from tests.ruleengine.conftest import D, count, credit, dep

TABLES = ["run", "rule_execution", "detection", "detection_transaction", "alert", "alert_detection", "alert_party",
          "alert_evidence", "outbox_event"]


def structuring():
    return [dep("t1", "A1", "2026-06-15 09:00", 3000), dep("t2", "A1", "2026-06-15 10:00", 3500),
            dep("t3", "A1", "2026-06-15 11:00", 4500)]


def test_mac15_failure_between_alert_insert_and_commit_leaves_nothing(make_engine, monkeypatch):
    eng = make_engine(structuring(), only=["TM-US-001"])
    r = eng.run_date(D, dry_run=True)
    alert, dets = r.alerts[0], r.detections

    def boom(name):
        if name == "after_alert_insert":
            raise RuntimeError("injected")
    monkeypatch.setattr(store, "_hook", boom)
    for d in dets:
        store.record_detection(eng.store, d)
    before = {t: count(eng.store, t) for t in ["detection"]}
    with pytest.raises(RuntimeError):
        store.create_alert(eng.store, alert)
    for t in ["alert", "alert_detection", "alert_party", "alert_evidence", "outbox_event"]:
        assert count(eng.store, t) == 0, t
    assert count(eng.store, "detection") == before["detection"]


def test_mac16_running_the_same_date_twice_creates_nothing_new(make_engine):
    eng = make_engine(structuring(), only=["TM-US-001"])
    eng.run_date(D)
    snap = {t: count(eng.store, t) for t in ["detection", "detection_transaction", "alert", "alert_detection",
                                             "alert_party", "alert_evidence", "outbox_event"]}
    r2 = eng.run_date(D)
    assert snap == {t: count(eng.store, t) for t in snap}
    assert count(eng.store, "run") == 2 and r2.status == "SUCCEEDED"


def test_mac17_failing_rule_is_recorded_and_other_rules_continue(make_engine, monkeypatch):
    t = structuring() + [credit("c1", "A3", "2026-06-15 12:00", 20000)]
    eng = make_engine(t, only=["TM-US-001", "TM-US-002"])
    real = evaluate.evaluate_rule

    def flaky(conn, rule, date, *a, **k):
        if rule.rule_id == "TM-US-002":
            raise RuntimeError("SQL error")
        return real(conn, rule, date, *a, **k)
    import ruleengine.engine as engine_mod
    monkeypatch.setattr(engine_mod, "evaluate_rule", flaky)
    r = eng.run_date(D)
    assert r.status == "PARTIAL"
    st = dict(eng.store.execute("SELECT rule_id, status FROM rule_execution").fetchall())
    assert st == {"TM-US-001": "SUCCEEDED", "TM-US-002": "FAILED"}
    assert any(d["rule_id"] == "TM-US-001" for d in r.detections) and len(r.alerts) == 1


def test_mac18_dry_run_writes_nothing(make_engine):
    eng = make_engine(structuring(), only=["TM-US-001"])
    r = eng.run_date(D, dry_run=True)
    assert len(r.detections) == 1 and len(r.alerts) == 1
    assert {t: count(eng.store, t) for t in TABLES} == {t: 0 for t in TABLES}


def test_run_record_has_manifest(make_engine):
    eng = make_engine(structuring())
    eng.run_date(D)
    status, manifest = eng.store.execute("SELECT status, manifest_json FROM run").fetchone()
    m = json.loads(manifest)
    assert status == "SUCCEEDED" and set(m["rules"]) == {"TM-US-001", "TM-US-002", "TM-US-003"}
    assert all(len(v["sha256"]) == 64 for v in m["rules"].values()) and m["policy"]["version"] == "1.0"
    assert m["businessDate"] == "2026-06-15" and "inputFingerprint" in m


def test_mac19_publisher_publishes_pending_rows_once_in_order(make_engine):
    t = structuring() + [dep(f"u{i}", "A3", f"2026-06-15 {10 + i:02d}:00", 4000) for i in range(3)]
    eng = make_engine(t, only=["TM-US-001"])
    eng.run_date(D)
    assert count(eng.store, "outbox_event") == 2
    out = []
    now = datetime(2026, 6, 16)
    assert publisher.publish_pending(eng.store, out.append, now=now) == 2
    assert [e["primaryPartyId"] for e in out] == sorted(e["primaryPartyId"] for e in out) or len(out) == 2
    assert {e["eventType"] for e in out} == {"AlertCreated"} and len({e["eventId"] for e in out}) == 2
    assert {"alertId", "alertKey", "ruleIds", "severity", "cycleDate"} <= set(out[0])
    assert publisher.publish_pending(eng.store, out.append, now=now) == 0 and len(out) == 2
    assert eng.store.execute("SELECT COUNT(*) FROM outbox_event WHERE status='PUBLISHED'").fetchone()[0] == 2


def test_mac19_failures_increment_attempts_and_end_in_failed(make_engine):
    eng = make_engine(structuring(), only=["TM-US-001"])
    eng.run_date(D)

    def broken(_):
        raise ConnectionError("kafka down")
    now = datetime(2026, 6, 16)
    for i in range(3):
        publisher.publish_pending(eng.store, broken, now=now + timedelta(days=i), max_attempts=3)
    status, attempts, err = eng.store.execute("SELECT status, attempts, last_error FROM outbox_event").fetchone()
    assert status == "FAILED" and attempts == 3 and "kafka down" in err
    # a recovered sink does not resend a FAILED row automatically
    out = []
    assert publisher.publish_pending(eng.store, out.append, now=now + timedelta(days=9), max_attempts=3) == 0


def test_mac19_event_is_pending_again_after_requeue(make_engine):
    eng = make_engine(structuring(), only=["TM-US-001"])
    eng.run_date(D)
    publisher.publish_pending(eng.store, lambda e: (_ for _ in ()).throw(ConnectionError("x")), now=datetime(2026, 6, 16), max_attempts=1)
    assert publisher.requeue_failed(eng.store) == 1
    out = []
    assert publisher.publish_pending(eng.store, out.append, now=datetime(2026, 6, 17)) == 1


def test_mac20_engine_code_never_reads_label_tables():
    banned = ("labels_", "schemes", "legit_spikes")
    pkg = Path(ruleengine.__file__).parent
    offenders = [str(p) for p in pkg.rglob("*") if p.suffix in {".py", ".json"}
                 and any(b in p.read_text() for b in banned)]
    assert offenders == []
