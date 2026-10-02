"""
evaluate.py - Measure detection against ground truth (labels are used ONLY here, never in detection).

Productive alert  = at least one triggering account carries suspicious activity (account-level truth).
                    This credits the alert when a joint holder - not the primary - is the bad actor.
Party precision   = the alerted primary party is itself a labelled bad actor.
Scheme recall     = share of laundering schemes with >= 1 of their accounts in a productive alert.
"""
import pandas as pd

import config as C
from scoring_alerts import score_parties


def evaluate(party_scores, ev, links, labels_accounts, labels_parties, schemes):
    sus_acct = labels_accounts[labels_accounts.is_suspicious]
    acct_schemes = {a: set(s.split(",")) for a, s in zip(sus_acct.account_id, sus_acct.scheme_ids)}
    al = party_scores[party_scores.alerted]
    e = ev[ev.primary_party_id.isin(al.party_id)].merge(
        al[["party_id", "cycle_month"]].rename(columns={"party_id": "primary_party_id"}),
        on=["primary_party_id", "cycle_month"])
    # accounts whose events contributed to each alert, incl. accounts of triggering transactions
    lk = links[links.link_role == "TRIGGER"].merge(e[["event_id", "primary_party_id", "cycle_month", "account_id"]],
                                                    on="event_id")
    detected_schemes, productive = set(), set()
    for (p, m), g in e.groupby(["primary_party_id", "cycle_month"]):
        hit = {a for a in g.account_id if a in acct_schemes}
        if hit:
            productive.add((p, m))
            for a in hit:
                detected_schemes |= acct_schemes[a]
    sus_party = set(labels_parties[labels_parties.is_suspicious].party_id)
    primary_bad = sus_party & set(party_scores.party_id)
    alerted_parties = set(al.party_id)
    n = len(al)
    return dict(
        alerts=n,
        alerted_parties=len(alerted_parties),
        productive_alert_rate=round(len(productive) / n, 3) if n else 0,
        party_precision=round(len(alerted_parties & sus_party) / max(len(alerted_parties), 1), 3),
        party_recall=round(len(alerted_parties & primary_bad) / len(primary_bad), 3),
        scheme_recall=round(len(detected_schemes) / len(schemes), 3),
        detected_schemes=detected_schemes,
    )


def compare(events, links, tx_product, parties, primary_ids, labels, thresholds):
    la, lp, sch = labels
    rows = []
    for variant, mask in (("Rules only", events.event_type == "RULE"),
                          ("ML only", events.event_type == "ML"),
                          ("Rules + ML", events.event_type.notna())):
        for thr in thresholds:
            ps, ev = score_parties(events[mask], links, tx_product, parties, primary_ids, thr)
            m = evaluate(ps, ev, links, la, lp, sch)
            m.pop("detected_schemes")
            rows.append(dict(variant=variant, threshold=thr, **m))
    return pd.DataFrame(rows)
