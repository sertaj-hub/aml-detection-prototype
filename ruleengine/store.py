"""Operational store (SQLite for the MVP; plain portable SQL for a later PostgreSQL move).

Alert, alert evidence, links and the outbox row are written in one transaction (FR-M9, FR-46).
Java analogy: a repository with a @Transactional createAlert().
"""
import json
import sqlite3
import uuid
from datetime import datetime

DDL = """
CREATE TABLE IF NOT EXISTS run(
  run_id TEXT PRIMARY KEY, cadence TEXT, business_date TEXT, mode TEXT, status TEXT, started_at TEXT, finished_at TEXT,
  manifest_json TEXT, counts_json TEXT, warnings_json TEXT);
CREATE TABLE IF NOT EXISTS rule_execution(
  run_id TEXT, rule_id TEXT, rule_version TEXT, status TEXT, error TEXT, detections INTEGER, started_at TEXT, finished_at TEXT,
  PRIMARY KEY(run_id, rule_id));
CREATE TABLE IF NOT EXISTS detection(
  detection_id TEXT PRIMARY KEY, rule_id TEXT, rule_version TEXT, entity_key TEXT, split_key TEXT, party_id TEXT,
  business_date TEXT, window_start TEXT, window_end TEXT, status TEXT, severity TEXT, ratio REAL, points REAL,
  metrics_json TEXT, evidence_json TEXT, run_id TEXT, alert_id TEXT);
CREATE TABLE IF NOT EXISTS detection_transaction(
  detection_id TEXT, txn_id TEXT, PRIMARY KEY(detection_id, txn_id));
CREATE INDEX IF NOT EXISTS ix_det_entity ON detection(rule_id, entity_key, split_key, business_date);
CREATE TABLE IF NOT EXISTS alert(
  alert_id TEXT PRIMARY KEY, alert_key TEXT UNIQUE, primary_party_id TEXT, cycle_type TEXT, cycle_date TEXT,
  policy_version TEXT, score REAL, threshold REAL, severity TEXT, status TEXT, run_id TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS alert_detection(alert_id TEXT, detection_id TEXT, PRIMARY KEY(alert_id, detection_id));
CREATE TABLE IF NOT EXISTS alert_party(alert_id TEXT, party_id TEXT, role TEXT, is_initiator INTEGER, PRIMARY KEY(alert_id, party_id));
CREATE TABLE IF NOT EXISTS alert_evidence(alert_id TEXT PRIMARY KEY, evidence_json TEXT);
CREATE TABLE IF NOT EXISTS outbox_event(
  event_id TEXT PRIMARY KEY, aggregate_id TEXT, event_type TEXT, payload_json TEXT, status TEXT, attempts INTEGER,
  next_attempt_at TEXT, created_at TEXT, published_at TEXT, last_error TEXT);
"""


def connect(path=":memory:"):
    return sqlite3.connect(path, isolation_level=None, check_same_thread=False)   # explicit transaction control


def init_schema(conn):
    for stmt in DDL.split(";"):
        if stmt.strip():
            conn.execute(stmt)


def _hook(name):
    """Fault-injection point for tests (e.g. 'after_alert_insert')."""


def _j(obj):
    return json.dumps(obj, default=str, sort_keys=True)


def record_detection(conn, d):
    """Insert a detection and its trigger links. Returns False when the detection id already exists (idempotent)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO detection VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
        [d["detection_id"], d["rule_id"], d["rule_version"], d["entity_key"], d["split_key"], d["party_id"],
         str(d["business_date"]), str(d["window_start"]), str(d["window_end"]), d["status"], d["severity"], d["ratio"],
         d.get("points"), _j(d["metrics"]), _j({k: d[k] for k in ("rule_name", "typology", "regulation", "explanation",
                                                                  "trigger_transactions", "accounts")}), d.get("run_id")])
    if cur.rowcount == 0:
        return False
    conn.executemany("INSERT OR IGNORE INTO detection_transaction VALUES (?,?)", [(d["detection_id"], t) for t in d["trigger_txn_ids"]])
    return True


def prior_trigger_ids(conn, rule_id, entity_key, split_key, before_date):
    rows = conn.execute(
        "SELECT DISTINCT dt.txn_id FROM detection d JOIN detection_transaction dt USING(detection_id) "
        "WHERE d.rule_id=? AND d.entity_key=? AND d.split_key=? AND d.business_date < ?",
        [rule_id, entity_key, split_key, str(before_date)]).fetchall()
    return {r[0] for r in rows}


def next_alert_seq(conn, cycle_date):
    return conn.execute("SELECT COUNT(*) FROM alert WHERE cycle_date=?", [str(cycle_date)]).fetchone()[0] + 1


def create_alert(conn, a):
    """Write alert + links + parties + evidence + outbox event atomically. Raises (and rolls back) on any failure."""
    now = datetime.utcnow().isoformat()
    event_id = "EVT-" + uuid.uuid4().hex[:16]
    payload = dict(a["event"], eventId=event_id, eventType="AlertCreated", eventVersion="1", createdAt=now)
    conn.execute("SAVEPOINT create_alert")
    try:
        conn.execute("INSERT INTO alert VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     [a["alert_id"], a["alert_key"], a["primary_party_id"], a["cycle_type"], str(a["cycle_date"]),
                      a["policy_version"], a["score"], a["threshold"], a["severity"], "GENERATED", a["run_id"], now])
        _hook("after_alert_insert")
        for did in a["detection_ids"]:
            conn.execute("INSERT INTO alert_detection VALUES (?,?)", [a["alert_id"], did])
            conn.execute("UPDATE detection SET alert_id=? WHERE detection_id=?", [a["alert_id"], did])
        for pid, role, init in a["parties"]:
            conn.execute("INSERT INTO alert_party VALUES (?,?,?,?)", [a["alert_id"], pid, role, 1 if init else 0])
        conn.execute("INSERT INTO alert_evidence VALUES (?,?)", [a["alert_id"], _j(a["evidence"])])
        conn.execute("INSERT INTO outbox_event VALUES (?,?,?,?,?,?,?,?,NULL,NULL)",
                     [event_id, a["alert_id"], "AlertCreated", _j(payload), "PENDING", 0, None, now])
        conn.execute("RELEASE create_alert")
    except BaseException:
        conn.execute("ROLLBACK TO create_alert")
        conn.execute("RELEASE create_alert")
        raise
    return event_id
