"""
run_pipeline.py - End-to-end: data -> rules -> ML events -> scoring -> alerts -> evaluation.

    python3 run_pipeline.py            # regenerate data and run everything
    python3 run_pipeline.py --no-data  # reuse existing data/ and rerun detection only
"""
import sys, time, warnings
import pandas as pd

import config as C
from rules_engine import RulesEngine, RULE_CATALOG
from ml_events import MLEventBuilder
from scoring_alerts import score_parties, build_alert_tables
from evaluate import evaluate, compare

warnings.filterwarnings("ignore")
OUT = "data"


def main(regenerate=True, thresholds=(20, 25, 30, 35, 40, 45, 50, 60)):
    t0 = time.time()
    if regenerate:
        from generate_data import build
        build(OUT)
        print(f"[1/4] synthetic data built ({time.time() - t0:.0f}s)")

    eng = RulesEngine(OUT)
    rule_ev, rule_lk = eng.run().frames()
    print(f"[2/4] rules: {len(rule_ev):,} events ({time.time() - t0:.0f}s)")
    ml = MLEventBuilder(eng)
    ml_ev, ml_lk = ml.run().frames()
    print(f"[3/4] ML: {len(ml_ev):,} events ({time.time() - t0:.0f}s)")

    events = pd.concat([rule_ev, ml_ev], ignore_index=True)
    links = pd.concat([rule_lk, ml_lk], ignore_index=True)
    tx_product = dict(zip(eng.tx.txn_id, eng.tx.product_type))
    primary_ids = sorted(set(eng.acct_party.values()))
    labels = tuple(pd.read_parquet(f"{OUT}/{n}.parquet") for n in ("labels_accounts", "labels_parties", "schemes"))

    cmp_ = compare(events, links, tx_product, eng.parties, primary_ids, labels, thresholds)
    print(cmp_.to_string(index=False))

    ps, ev = score_parties(events, links, tx_product, eng.parties, primary_ids, C.ALERT_THRESHOLD)
    alerts, alert_accounts, alert_events, alert_txns = build_alert_tables(
        ev, links, ps, eng.tx, eng.accounts, eng.parties.reset_index(), eng.roles,
        eng.cps.reset_index(), RULE_CATALOG)
    print(f"[4/4] {len(alerts):,} alerts at threshold {C.ALERT_THRESHOLD} ({time.time() - t0:.0f}s)")

    model_catalog = pd.DataFrame(ml.catalog)
    out = dict(rule_catalog=RULE_CATALOG, ml_model_catalog=model_catalog, ml_features=pd.concat(ml.features_out),
               detection_events=ev.drop(columns=[], errors="ignore"), event_transactions=links,
               party_scores=ps, alerts=alerts, alert_accounts=alert_accounts, alert_events=alert_events,
               alert_transactions=alert_txns, evaluation_threshold_sweep=cmp_)
    for k, v in out.items():
        v.to_parquet(f"{OUT}/{k}.parquet", index=False)
    m = evaluate(ps, ev, links, *labels)
    m.pop("detected_schemes")
    print("chosen threshold:", m)
    return out


if __name__ == "__main__":
    main(regenerate="--no-data" not in sys.argv)
