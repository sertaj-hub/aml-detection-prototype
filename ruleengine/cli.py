"""Command line: run / validate / publish.

    python -m ruleengine validate ruleengine/rules/TM-US-001.json
    python -m ruleengine run --date 2026-06-15 [--dry-run]
    python -m ruleengine run --from 2026-04-01 --to 2026-09-30
    python -m ruleengine publish
"""
import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from ruleengine import publisher, store
from ruleengine.data import open_parquet
from ruleengine.engine import Engine
from ruleengine.policy import load_policy
from ruleengine.rules import RuleValidationError, load_rule, load_rules_dir

PKG = Path(__file__).parent


def _date(s):
    return date.fromisoformat(s)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ruleengine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate")
    v.add_argument("files", nargs="+")
    r = sub.add_parser("run")
    r.add_argument("--date", type=_date)
    r.add_argument("--from", dest="start", type=_date)
    r.add_argument("--to", dest="end", type=_date)
    r.add_argument("--data-dir", default="data")
    r.add_argument("--rules", default=str(PKG / "rules"))
    r.add_argument("--policy", default=str(PKG / "policy" / "mvp_policy.json"))
    r.add_argument("--db", default="out/engine.db")
    r.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("publish")
    p.add_argument("--db", default="out/engine.db")
    p.add_argument("--out", default="out/alerts.jsonl")
    a = ap.parse_args(argv)

    if a.cmd == "validate":
        bad = 0
        for f in a.files:
            try:
                rule = load_rule(f)
                print(f"OK    {f}  {rule.rule_id} v{rule.version}")
            except RuleValidationError as e:
                bad += 1
                print(f"FAIL  {f}")
                for path, msg in e.errors:
                    print(f"        {path}: {msg}")
        return 1 if bad else 0

    if a.cmd == "publish":
        conn = store.connect(a.db)
        n = publisher.publish_pending(conn, publisher.file_sink(a.out))
        print(f"published {n} event(s) to {a.out}")
        return 0

    days = [a.date] if a.date else []
    if not days:
        if not (a.start and a.end):
            ap.error("give --date, or --from and --to")
        days = [a.start + timedelta(days=i) for i in range((a.end - a.start).days + 1)]
    Path(a.db).parent.mkdir(parents=True, exist_ok=True)
    sconn = store.connect(a.db)
    store.init_schema(sconn)
    eng = Engine(open_parquet(a.data_dir), load_rules_dir(a.rules), load_policy(a.policy), sconn)
    t0 = time.time()
    tot = {"detections": 0, "suppressed": 0, "alerts": 0}
    for d in days:
        res = eng.run_date(d, dry_run=a.dry_run)
        for k in tot:
            tot[k] += res.counts[k]
        if len(days) == 1 or res.counts["alerts"] or res.status != "SUCCEEDED":
            print(f"{d}  {res.status:<9} detections={res.counts['detections']} suppressed={res.counts['suppressed']} "
                  f"alerts={res.counts['alerts']}" + (f"  warnings={res.warnings}" if res.warnings else ""))
        if a.dry_run and len(days) == 1:
            for al in res.alerts:
                print(f"  [dry-run] {al['alert_id']} party={al['primary_party_id']} score={al['score']} severity={al['severity']}")
                for det in al["evidence"]["detections"]:
                    print(f"      {det['ruleId']} {det['points']} pts: {det['explanation']}")
    print(f"done: {len(days)} day(s), {tot['detections']} detections ({tot['suppressed']} suppressed), "
          f"{tot['alerts']} alerts in {time.time() - t0:.1f}s" + ("  [dry run: nothing written]" if a.dry_run else f"  -> {a.db}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
