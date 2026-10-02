"""
scoring_alerts.py - Roll detection events up into party-level alerts with full drill-down.

Score hierarchy (everything adds up, so investigators can see exactly what crossed the line):
  transaction contribution  ->  event effective points  ->  account score  ->  alert (party) score

  event effective points : rule/ML points x repeat discount (same scenario repeating on the
                           same account in the cycle: strongest counts 100%, each repeat 25%,
                           capped at 2x the strongest)
  account score          : sum of effective points of events attributed to that account
  alert score            : sum of account scores + cross-product bonus + KYC high-risk bonus
  alert                  : alert score >= ALERT_THRESHOLD (one alert per primary party per monthly cycle)

Alerts are raised on the PRIMARY party of the triggering accounts. Secondary holders (joint,
authorised user, co-borrower, guarantor) on those accounts are linked for investigator context.
"""
import numpy as np
import pandas as pd

import config as C


def effective_points(events):
    """Apply repeat discount per (account, cycle, source). Java: group, sort desc, fold."""
    ev = events.sort_values(["account_id", "cycle_month", "source_id", "points"],
                            ascending=[True, True, True, False]).copy()
    ev["rank"] = ev.groupby(["account_id", "cycle_month", "source_id"]).cumcount()
    ev["raw_eff"] = np.where(ev["rank"] == 0, ev.points, ev.points * C.RULE_REPEAT_FACTOR)
    grp = ev.groupby(["account_id", "cycle_month", "source_id"])
    cap = grp.points.transform("max") * C.RULE_REPEAT_CAP
    total = grp.raw_eff.transform("sum")
    scale = np.minimum(1.0, cap / total)
    ev["effective_points"] = (ev.raw_eff * scale).round(2)
    return ev.drop(columns=["rank", "raw_eff"])


def score_parties(events, links, tx_product, parties, primary_party_ids, threshold=C.ALERT_THRESHOLD):
    """Return party_scores (every primary party x month) for a set of events."""
    ev = effective_points(events)
    # products touched by each party-month (via the accounts of all triggering transactions)
    lk = links[links.link_role == "TRIGGER"].merge(ev[["event_id", "primary_party_id", "cycle_month"]], on="event_id")
    lk["txn_product"] = lk.txn_id.map(tx_product)
    prods = lk.groupby(["primary_party_id", "cycle_month"]).txn_product.agg(lambda s: set(s.dropna()))

    agg = ev.groupby(["primary_party_id", "cycle_month"]).agg(
        rule_points=("effective_points", lambda s: s[ev.loc[s.index, "event_type"] == "RULE"].sum()),
        ml_points=("effective_points", lambda s: s[ev.loc[s.index, "event_type"] == "ML"].sum()),
        n_rule_events=("event_type", lambda s: (s == "RULE").sum()),
        n_ml_events=("event_type", lambda s: (s == "ML").sum()),
        n_accounts=("account_id", "nunique")).reset_index()
    agg["products"] = [",".join(sorted(prods.get((p, m), set()))) for p, m in zip(agg.primary_party_id, agg.cycle_month)]
    n_prod = agg.products.str.count(",") + (agg.products != "").astype(int)
    agg["cross_product_bonus"] = n_prod.map(lambda n: C.CROSS_PRODUCT_BONUS.get(min(n, 3), 0))
    kyc = agg.primary_party_id.map(parties.kyc_risk)
    agg["kyc_bonus"] = np.where(kyc == "HIGH", C.KYC_HIGH_RISK_BONUS, 0)
    agg["score"] = (agg.rule_points + agg.ml_points + agg.cross_product_bonus + agg.kyc_bonus).round(1)

    # every primary party x every month, alerted or not
    grid = pd.MultiIndex.from_product([sorted(primary_party_ids), C.MONTHS],
                                      names=["primary_party_id", "cycle_month"]).to_frame(index=False)
    ps = grid.merge(agg, how="left", on=["primary_party_id", "cycle_month"])
    num = ["rule_points", "ml_points", "n_rule_events", "n_ml_events", "n_accounts", "cross_product_bonus",
           "kyc_bonus", "score"]
    ps[num] = ps[num].fillna(0)
    ps["products"] = ps.products.fillna("")
    ps["threshold"] = threshold
    ps["alerted"] = ps.score >= threshold
    ps["near_miss"] = (~ps.alerted) & (ps.score >= threshold - C.NEAR_MISS_BAND)
    return ps.rename(columns={"primary_party_id": "party_id"}), ev


def build_alert_tables(ev, links, party_scores, tx, accounts, parties, roles, cps, rule_catalog):
    """Materialise alerts + drill-down tables for alerted party-months."""
    al = party_scores[party_scores.alerted].copy().sort_values(["cycle_month", "score"], ascending=[True, False])
    al["alert_id"] = [f"A-{i:05d}" for i in range(1, len(al) + 1)]
    key = al.set_index(["party_id", "cycle_month"]).alert_id
    ev = ev.copy()
    ev["alert_id"] = [key.get((p, m)) for p, m in zip(ev.primary_party_id, ev.cycle_month)]
    aev = ev[ev.alert_id.notna()]

    acct = accounts.set_index("account_id")
    rcat = rule_catalog.set_index("rule_id")
    secondaries = roles[roles.role != "PRIMARY"].groupby("account_id").apply(
        lambda d: "; ".join(f"{p} ({r})" for p, r in zip(d.party_id, d.role)), include_groups=False)

    # ---------------- alert_events
    alert_events = aev[["alert_id", "event_id", "event_type", "source_id", "source_name", "account_id",
                        "product_type", "fire_ts", "window_start", "observed", "threshold", "ratio_to_threshold",
                        "severity", "points", "effective_points", "model_score", "reason_codes"]].copy()
    alert_events["scenario_typology"] = alert_events.source_id.map(rcat.typology).fillna("BEHAVIOUR_ANOMALY")

    # ---------------- alert_accounts (triggering + other accounts of the party for context)
    acc_scores = aev.groupby(["alert_id", "account_id"]).agg(
        account_score=("effective_points", "sum"), n_events=("event_id", "size"),
        scenarios_hit=("source_id", lambda s: ", ".join(sorted(set(s))))).reset_index()
    acc_scores["is_triggering"] = True
    prim = roles[roles.role == "PRIMARY"]
    ctx_rows = []
    for _, a in al.iterrows():
        for aid in prim[prim.party_id == a.party_id].account_id:
            if not ((acc_scores.alert_id == a.alert_id) & (acc_scores.account_id == aid)).any():
                ctx_rows.append(dict(alert_id=a.alert_id, account_id=aid, account_score=0.0, n_events=0,
                                     scenarios_hit="", is_triggering=False))
    alert_accounts = pd.concat([acc_scores, pd.DataFrame(ctx_rows)], ignore_index=True)
    alert_accounts["product_type"] = alert_accounts.account_id.map(acct.product_type)
    alert_accounts["product_subtype"] = alert_accounts.account_id.map(acct.product_subtype)
    alert_accounts["party_role"] = "PRIMARY"
    alert_accounts["open_date"] = alert_accounts.account_id.map(acct.open_date)
    alert_accounts["status"] = alert_accounts.account_id.map(acct.status)
    alert_accounts["linked_secondary_parties"] = alert_accounts.account_id.map(secondaries).fillna("")
    tot = alert_accounts.alert_id.map(al.set_index("alert_id").score)
    alert_accounts["pct_of_alert_score"] = (alert_accounts.account_score / tot).round(3)
    alert_accounts = alert_accounts.sort_values(["alert_id", "account_score"], ascending=[True, False])

    # ---------------- alert_transactions (scaled so contributions sum to effective event points)
    lk = links.merge(aev[["event_id", "alert_id", "points", "effective_points", "source_id", "account_id",
                          "observed", "reason_codes", "event_type"]].rename(columns={"account_id": "event_account_id"}),
                     on="event_id")
    lk["points_contribution"] = (lk.points_contribution * lk.effective_points / lk.points).round(2)
    txc = tx.set_index("txn_id")
    cpn = cps.set_index("counterparty_id").cp_name
    cols = ["account_id", "product_type", "txn_ts", "txn_type", "direction", "amount", "channel", "is_cash",
            "counterparty_id", "cp_country", "mcc", "location_state", "initiated_by_party_id", "description"]
    lk = lk.join(txc[cols], on="txn_id")
    lk["counterparty_name"] = lk.counterparty_id.map(cpn)
    lk["reason"] = np.where(lk.event_type == "RULE", lk.observed, lk.reason_codes)
    lk["txn_score_in_alert"] = lk.groupby(["alert_id", "txn_id"]).points_contribution.transform("sum").round(2)
    alert_transactions = lk[["alert_id", "event_id", "source_id", "event_account_id", "txn_id", "link_role",
                             "points_contribution", "txn_score_in_alert"] + cols + ["counterparty_name", "reason"]] \
        .sort_values(["alert_id", "event_id", "txn_ts"])

    # ---------------- alerts (header)
    pt = parties.set_index("party_id")
    names = aev.groupby("alert_id").source_id.agg(lambda s: ", ".join(sorted(set(s))))
    trig_accts = aev.groupby("alert_id").account_id.nunique()
    sec = alert_accounts[alert_accounts.is_triggering].groupby("alert_id").linked_secondary_parties.agg(
        lambda s: "; ".join(x for x in s if x))
    alerts = pd.DataFrame({
        "alert_id": al.alert_id, "party_id": al.party_id, "cycle_month": al.cycle_month,
        "alert_score": al.score, "threshold": al.threshold,
        "rule_points": al.rule_points.round(1), "ml_points": al.ml_points.round(1),
        "cross_product_bonus": al.cross_product_bonus, "kyc_bonus": al.kyc_bonus,
        "products_involved": al.products, "n_rule_events": al.n_rule_events.astype(int),
        "n_ml_events": al.n_ml_events.astype(int)})
    alerts["scenarios_hit"] = alerts.alert_id.map(names)
    alerts["n_triggering_accounts"] = alerts.alert_id.map(trig_accts)
    alerts["linked_secondary_parties"] = alerts.alert_id.map(sec).fillna("")
    for c in ["first_name", "last_name", "dob", "segment", "occupation", "annual_income", "kyc_risk", "pep_flag",
              "state", "onboarding_date", "expected_monthly_credits", "expected_monthly_cash"]:
        alerts[f"party_{c}"] = alerts.party_id.map(pt[c])
    alerts["status"] = "OPEN"
    alerts["created_date"] = (pd.to_datetime(alerts.cycle_month + "-01") + pd.offsets.MonthEnd(0)
                              + pd.Timedelta(days=1)).dt.date
    return alerts.reset_index(drop=True), alert_accounts, alert_events, alert_transactions
