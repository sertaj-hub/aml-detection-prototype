"""
show_alert.py - Print one alert as an investigator drill-down tree.

    python3 show_alert.py A-00042        # a specific alert
    python3 show_alert.py --top 3        # the 3 highest-scoring alerts
"""
import sys
import pandas as pd

d = lambda n: pd.read_parquet(f"data/{n}.parquet")
A, AA, AE, AT = d("alerts"), d("alert_accounts"), d("alert_events"), d("alert_transactions")


def show(alert_id, max_txns=6):
    a = A[A.alert_id == alert_id].iloc[0]
    print(f"ALERT {a.alert_id} | {a.cycle_month} | Party {a.party_id} {a.party_first_name} {a.party_last_name} "
          f"| Score {a.alert_score:.0f} / threshold {a.threshold:.0f}")
    print(f"  {a.party_segment}, {a.party_occupation}, KYC {a.party_kyc_risk}, income ${a.party_annual_income:,.0f}, "
          f"expected cash ${a.party_expected_monthly_cash:,.0f}/mo, customer since {a.party_onboarding_date}")
    if a.linked_secondary_parties:
        print(f"  Linked secondary holders: {a.linked_secondary_parties}")
    for _, acc in AA[AA.alert_id == alert_id].iterrows():
        if not acc.is_triggering:
            print(f"├─ {acc.product_subtype} {acc.account_id} (no hits)")
            continue
        print(f"├─ {acc.product_subtype} {acc.account_id}  +{acc.account_score:.1f}  ({acc.pct_of_alert_score:.0%} of alert)")
        for _, e in AE[(AE.alert_id == alert_id) & (AE.account_id == acc.account_id)].iterrows():
            print(f"│  ├─ {e.event_type} {e.source_id} {e.source_name}  +{e.effective_points:.1f}")
            print(f"│  │   {e.observed}")
            if e.event_type == "RULE":
                print(f"│  │   (rule: {e.threshold})")
            else:
                print(f"│  │   Reasons: {e.reason_codes}")
            tx = AT[(AT.alert_id == alert_id) & (AT.event_id == e.event_id) & (AT.link_role == "TRIGGER")]
            for _, t in tx.nlargest(max_txns, "points_contribution").sort_values("txn_ts").iterrows():
                cp = f" -> {t.counterparty_name} ({t.cp_country})" if isinstance(t.counterparty_name, str) else ""
                if isinstance(t.initiated_by_party_id, str) and t.initiated_by_party_id != a.party_id:
                    cp += f"  by {t.initiated_by_party_id}"   # someone other than the alerted party acted
                print(f"│  │   └─ {t.txn_id} {t.txn_ts:%Y-%m-%d} {t.txn_type:<22} ${t.amount:>10,.2f} "
                      f"{t.channel:<8}{cp}  [+{t.points_contribution:.1f}]")
            if len(tx) > max_txns:
                print(f"│  │   └─ ... {len(tx) - max_txns} more transactions")
    if a.cross_product_bonus:
        print(f"├─ Cross-product bonus +{a.cross_product_bonus:.0f} ({a.products_involved})")
    if a.kyc_bonus:
        print(f"└─ KYC high-risk bonus +{a.kyc_bonus:.0f}")
    print()


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--top":
        for aid in A.nlargest(int(sys.argv[2]), "alert_score").alert_id:
            show(aid)
    else:
        show(sys.argv[1] if len(sys.argv) > 1 else A.alert_id.iloc[0])
