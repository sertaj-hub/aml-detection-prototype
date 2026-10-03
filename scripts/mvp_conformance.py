"""MAC-21/22: run the MVP engine over the prototype's six months and compare with the prototype and the labels.

This script is the ONLY place labels are read (evaluation). The engine package never touches them.

    python3 scripts/mvp_conformance.py [--rules ruleengine/rules] [--db out/conformance.db] [--keep]
"""
import argparse
import json
import os
import time
from datetime import date, timedelta
from pathlib import Path

import duckdb

from ruleengine import store
from ruleengine.data import open_parquet
from ruleengine.engine import Engine
from ruleengine.policy import load_policy
from ruleengine.rules import load_rules_dir

ap = argparse.ArgumentParser()
ap.add_argument("--rules", default="ruleengine/rules")
ap.add_argument("--policy", default="ruleengine/policy/mvp_policy.json")
ap.add_argument("--db", default="out/conformance.db")
ap.add_argument("--from", dest="start", default="2026-04-01")
ap.add_argument("--to", dest="end", default="2026-09-30")
ap.add_argument("--reuse", action="store_true", help="evaluate an existing --db without re-running")
args = ap.parse_args()

Path(args.db).parent.mkdir(parents=True, exist_ok=True)
if os.path.exists(args.db) and not args.reuse:
    os.remove(args.db)
sconn = store.connect(args.db)
store.init_schema(sconn)
rules = load_rules_dir(args.rules)
eng = Engine(open_parquet("data"), rules, load_policy(args.policy), sconn)
d0, d1 = date.fromisoformat(args.start), date.fromisoformat(args.end)
t0 = time.time()
failed = 0
if not args.reuse:
    for i in range((d1 - d0).days + 1):
        r = eng.run_date(d0 + timedelta(days=i))
        failed += r.status != "SUCCEEDED"
elapsed = time.time() - t0

q = lambda sql, *p: sconn.execute(sql, p).fetchall()
print(f"\nMAC-22  runtime {elapsed:.1f}s for {(d1 - d0).days + 1} days; runs not SUCCEEDED: {failed}")
print("detections by rule/status:")
for row in q("SELECT rule_id, status, COUNT(*) FROM detection GROUP BY 1,2 ORDER BY 1,2"):
    print("   ", row)
n_alert, n_party = q("SELECT COUNT(*), COUNT(DISTINCT primary_party_id) FROM alert")[0]
print(f"alerts: {n_alert} for {n_party} distinct parties at threshold {eng.policy['dailyThreshold']}")
print("alerts per rule combination:")
for row in q("SELECT combo, COUNT(*) FROM (SELECT alert_id, GROUP_CONCAT(rule_id) combo FROM "
             "(SELECT DISTINCT a.alert_id, d.rule_id FROM alert a JOIN detection d USING(alert_id) ORDER BY 1, 2) GROUP BY alert_id) "
             "GROUP BY combo ORDER BY 2 DESC"):
    print("   ", row)

# ---- evaluation against labels (not part of the engine)
L = duckdb.connect()
bad_parties = {r[0] for r in L.execute("SELECT party_id FROM 'data/labels_parties.parquet' WHERE is_suspicious").fetchall()}
bad_accts = {r[0] for r in L.execute("SELECT account_id FROM 'data/labels_accounts.parquet' WHERE is_suspicious").fetchall()}
print(f"\nlabelled suspicious parties: {len(bad_parties)}; accounts: {len(bad_accts)}")
print("per rule: detections / distinct detected parties / of which labelled suspicious")
for rid, in q("SELECT DISTINCT rule_id FROM detection ORDER BY 1"):
    ps = {r[0] for r in q("SELECT DISTINCT party_id FROM detection WHERE rule_id=? AND status<>'SUPPRESSED_NO_NEW_EVIDENCE'", rid)}
    nd = q("SELECT COUNT(*) FROM detection WHERE rule_id=?", rid)[0][0]
    print(f"   {rid}: {nd} detections, {len(ps)} parties, {len(ps & bad_parties)} labelled suspicious "
          f"({(len(ps & bad_parties) / len(ps) if ps else 0):.0%})")
alerted = {r[0] for r in q("SELECT DISTINCT primary_party_id FROM alert")}
print(f"alerted parties {len(alerted)}: {len(alerted & bad_parties)} labelled suspicious "
      f"(precision {len(alerted & bad_parties) / max(len(alerted), 1):.0%}, recall {len(alerted & bad_parties) / len(bad_parties):.0%})")

# ---- MAC-21: TM-US-001 vs the prototype's R-DEP-01
proto = {r[0] for r in L.execute("SELECT DISTINCT account_id FROM 'data/detection_events.parquet' WHERE source_id='R-DEP-01'").fetchall()}
eng_accts = set()
for (ev,) in q("SELECT evidence_json FROM detection WHERE rule_id='TM-US-001'"):
    eng_accts |= {t["account_id"] for t in json.loads(ev)["trigger_transactions"]}
both, po, eo = proto & eng_accts, proto - eng_accts, eng_accts - proto
print(f"\nMAC-21  accounts: prototype R-DEP-01 {len(proto)}, engine TM-US-001 {len(eng_accts)}; both {len(both)}, "
      f"only prototype {len(po)}, only engine {len(eo)}")
for name, s in (("prototype", proto), ("engine", eng_accts)):
    print(f"   {name}: labelled-suspicious accounts {len(s & bad_accts)}/{len(s)} ({len(s & bad_accts) / max(len(s), 1):.0%})")
