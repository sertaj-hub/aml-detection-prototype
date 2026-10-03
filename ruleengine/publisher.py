"""Outbox publisher stub (FR-M13): at-least-once delivery with attempts, backoff and FAILED state.

The MVP sink writes JSON lines to a file; a Kafka producer would plug in as the same ``sink(payload)`` callable.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path


def publish_pending(conn, sink, now=None, max_attempts=5):
    now = now or datetime.utcnow()
    rows = conn.execute(
        "SELECT event_id, payload_json, attempts FROM outbox_event WHERE status='PENDING' "
        "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY rowid", [now.isoformat()]).fetchall()
    published = 0
    for event_id, payload_json, attempts in rows:
        try:
            sink(json.loads(payload_json))
        except Exception as e:                                  # delivery failure: count it, back off, eventually FAILED
            attempts += 1
            if attempts >= max_attempts:
                conn.execute("UPDATE outbox_event SET status='FAILED', attempts=?, last_error=? WHERE event_id=?",
                             [attempts, str(e), event_id])
            else:
                nxt = (now + timedelta(seconds=min(2 ** attempts, 3600))).isoformat()
                conn.execute("UPDATE outbox_event SET attempts=?, next_attempt_at=?, last_error=? WHERE event_id=?",
                             [attempts, nxt, str(e), event_id])
            continue
        conn.execute("UPDATE outbox_event SET status='PUBLISHED', published_at=? WHERE event_id=?", [now.isoformat(), event_id])
        published += 1
    return published


def requeue_failed(conn):
    cur = conn.execute("UPDATE outbox_event SET status='PENDING', attempts=0, next_attempt_at=NULL WHERE status='FAILED'")
    return cur.rowcount


def file_sink(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def write(payload):
        with path.open("a") as f:
            f.write(json.dumps(payload, sort_keys=True) + "\n")
    return write
