"""
ml_events.py - Unsupervised anomaly detection per product, emitting ML DETECTION EVENTS.

For each product (deposit, card, loan):
  1. Build account-month behavioural features (incl. ratios to the party's KYC profile)
  2. Fit an IsolationForest (no labels used - unsupervised)
  3. Account-months above the 99th anomaly percentile raise an ML event
  4. SHAP explains WHY: top features pushing the score up become plain-English reason codes
  5. The transactions behind those top features are linked to the event (drill-down)

Java analogy: a FeatureBuilder per product + a Model wrapper; pandas groupby/agg is the
equivalent of Collectors.groupingBy(..., Collectors.summingDouble(...)) on a stream.
"""
import numpy as np
import pandas as pd
import shap
from sklearn.ensemble import IsolationForest

import config as C
from rules_engine import EventSink, NAMES

MODELS = {
    "DEPOSIT": ("M-ANOM-DEP", "Deposit behaviour anomaly"),
    "CARD": ("M-ANOM-CRD", "Card behaviour anomaly"),
    "LOAN": ("M-ANOM-LN", "Loan repayment anomaly"),
}

NAMES.update({mid: name for mid, name in MODELS.values()})   # readable names on ML events

# feature -> (reason-code label, value format, transaction filter for drill-down)
FEATURES = {
    "DEPOSIT": {
        "f_credits_vs_expected": ("External credits vs declared income", "x",
                                  lambda t: (t.direction == "CR") & (t.channel != "INTERNAL") & (t.amount >= 500)),
        "f_cash_in_vs_expected": ("Cash deposits vs expected cash", "x", lambda t: t.txn_type == "CASH_DEPOSIT"),
        "f_cash_in_amt": ("Cash deposited", "$", lambda t: t.txn_type == "CASH_DEPOSIT"),
        "f_n_cash_band": ("Cash deposits of $8k-$10k", "n",
                          lambda t: (t.txn_type == "CASH_DEPOSIT") & t.amount.between(8000, 9999.99)),
        "f_n_cash_states": ("States where cash was deposited", "n", lambda t: t.txn_type == "CASH_DEPOSIT"),
        "f_n_inbound_sources": ("Distinct inbound senders", "n",
                                lambda t: t.txn_type.isin(["P2P_IN", "ACH_CREDIT", "CASH_DEPOSIT", "WIRE_IN"])),
        "f_pass_through": ("Share of credits moved straight out", "%",
                           lambda t: t.txn_type.isin(["WIRE_OUT", "P2P_OUT", "CHECK_PAID", "ATM_WITHDRAWAL",
                                                      "WIRE_IN", "P2P_IN"]) & (t.amount >= 300)),
        "f_wire_in_amt": ("Incoming wires", "$", lambda t: t.txn_type == "WIRE_IN"),
        "f_wire_out_amt": ("Outgoing wires", "$", lambda t: t.txn_type == "WIRE_OUT"),
        "f_intl_wire_amt": ("International wires", "$",
                            lambda t: t.txn_type.isin(["WIRE_IN", "WIRE_OUT"]) & (t.cp_country != "US")),
        "f_hr_wire_amt": ("Wires with high-risk countries", "$",
                          lambda t: t.cp_country.isin(C.HIGH_RISK_COUNTRIES)),
        "f_max_credit_vs_expected": ("Largest single credit vs monthly income", "x",
                                     lambda t: (t.direction == "CR") & (t.channel != "INTERNAL")),
        "f_dormancy_days": ("Days inactive before this month", "n", lambda t: t.channel != "SYSTEM"),
    },
    "CARD": {
        "f_purchase_vs_limit": ("Purchases vs credit limit", "x", lambda t: t.txn_type == "PURCHASE"),
        "f_spend_vs_usual": ("Spend vs usual month", "x", lambda t: t.txn_type == "PURCHASE"),
        "f_cash_adv_amt": ("Cash advances", "$", lambda t: t.txn_type == "CASH_ADVANCE"),
        "f_n_cash_adv": ("Number of cash advances", "n", lambda t: t.txn_type == "CASH_ADVANCE"),
        "f_payments_vs_limit": ("Payments vs credit limit", "x", lambda t: t.txn_type == "PAYMENT"),
        "f_cash_payment_amt": ("Card paid in cash", "$", lambda t: (t.txn_type == "PAYMENT") & t.is_cash),
        "f_third_party_pay_amt": ("Payments from third parties", "$", lambda t: t.is_third_party),
        "f_n_third_party_payors": ("Distinct third-party payors", "n", lambda t: t.is_third_party),
        "f_hr_mcc_amt": ("Gambling / crypto spend", "$", lambda t: t.mcc.isin(C.HIGH_RISK_MCC)),
        "f_hr_mcc_share": ("Gambling / crypto share of spend", "%", lambda t: t.mcc.isin(C.HIGH_RISK_MCC)),
        "f_cb_refund_amt": ("Credit-balance refunds", "$", lambda t: t.txn_type == "CREDIT_BALANCE_REFUND"),
        "f_returned_pay_amt": ("Returned payments", "$", lambda t: t.txn_type == "RETURNED_PAYMENT"),
    },
    "LOAN": {
        "f_payments_vs_scheduled": ("Repaid vs scheduled payment", "x", lambda t: t.direction == "CR"),
        "f_extra_vs_loan": ("Extra / payoff vs loan amount", "%",
                            lambda t: t.txn_type.isin(["EXTRA_PAYMENT", "PAYOFF"])),
        "f_n_third_party_pay": ("Payments from third parties", "n", lambda t: t.is_third_party),
        "f_cash_pay_amt": ("Loan paid in cash", "$", lambda t: (t.direction == "CR") & t.is_cash),
        "f_months_since_open": ("Months since origination", "n", lambda t: t.txn_type == "DISBURSEMENT"),
        "f_disb_outflow_ratio": ("Loan proceeds moved out within 7 days", "%",
                                 lambda t: t.txn_type == "DISBURSEMENT"),
    },
}
LOG_FEATURES = {"f_cash_in_amt", "f_wire_in_amt", "f_wire_out_amt", "f_intl_wire_amt", "f_hr_wire_amt",
                "f_cash_adv_amt", "f_cash_payment_amt", "f_third_party_pay_amt", "f_hr_mcc_amt",
                "f_cb_refund_amt", "f_returned_pay_amt", "f_cash_pay_amt"}


def fmt(v, kind):
    if kind == "$":
        return f"${v:,.0f}"
    if kind == "%":
        return f"{v:.0%}"
    if kind == "x":
        return f"{v:.1f}x"
    return f"{v:,.0f}"


class MLEventBuilder:
    def __init__(self, eng):
        """eng: a loaded RulesEngine (re-uses its data and lookups)."""
        self.e = eng
        self.tx = eng.tx
        self.tx["is_third_party"] = eng.third_party_mask(self.tx)
        self.sink = EventSink(eng.acct_party, eng.acct_product, prefix="ME")
        self.catalog, self.features_out = [], []

    # ----------------------------------------------------------- features
    def deposit_features(self):
        e, t = self.e, self.tx[(self.tx.product_type == "DEPOSIT") & (self.tx.channel != "SYSTEM")].copy()
        t["ext_cr"] = t.amount.where((t.direction == "CR") & (t.channel != "INTERNAL"), 0)
        t["cash_in"] = t.amount.where(t.txn_type == "CASH_DEPOSIT", 0)
        t["cash_band"] = ((t.txn_type == "CASH_DEPOSIT") & t.amount.between(8000, 9999.99)).astype(int)
        t["is_cash_in"] = (t.txn_type == "CASH_DEPOSIT").astype(int)
        t["nonroutine_dr"] = t.amount.where((t.direction == "DR") & ~t.txn_type.isin(
            ["POS_PURCHASE", "ACH_DEBIT", "CARD_PAYMENT_OUT", "LOAN_PAYMENT_OUT", "TRANSFER_OUT"]), 0)
        t["wire_in"] = t.amount.where(t.txn_type == "WIRE_IN", 0)
        t["wire_out"] = t.amount.where(t.txn_type == "WIRE_OUT", 0)
        t["intl"] = t.amount.where(t.txn_type.isin(["WIRE_IN", "WIRE_OUT"]) & (t.cp_country != "US"), 0)
        t["hr"] = t.amount.where(t.cp_country.isin(C.HIGH_RISK_COUNTRIES), 0)
        g = t.groupby(["account_id", "month"])
        f = g.agg(ext_cr=("ext_cr", "sum"), cash_in=("cash_in", "sum"), f_n_cash_band=("cash_band", "sum"),
                  n_cash_in=("is_cash_in", "sum"), nonroutine_dr=("nonroutine_dr", "sum"),
                  f_wire_in_amt=("wire_in", "sum"), f_wire_out_amt=("wire_out", "sum"),
                  f_intl_wire_amt=("intl", "sum"), f_hr_wire_amt=("hr", "sum"), max_cr=("ext_cr", "max"),
                  first_ts=("txn_ts", "min"))
        cash = t[t.txn_type == "CASH_DEPOSIT"]
        f["f_n_cash_states"] = cash.groupby(["account_id", "month"]).location_state.nunique()
        src = t[(t.direction == "CR") & (t.channel != "INTERNAL") & (t.counterparty_id.notna() | t.counter_account_id.notna())]
        f["f_n_inbound_sources"] = src.assign(s=src.counterparty_id.fillna(src.counter_account_id)) \
                                      .groupby(["account_id", "month"]).s.nunique()
        f = f.fillna(0).reset_index()
        party = f.account_id.map(e.acct_party)
        exp_cr = party.map(e.parties.expected_monthly_credits).clip(lower=500)
        exp_cash = party.map(e.parties.expected_monthly_cash).clip(lower=100)
        f["f_credits_vs_expected"] = f.ext_cr / exp_cr
        f["f_cash_in_vs_expected"] = f.cash_in / exp_cash
        f["f_cash_in_amt"] = f.cash_in
        f["f_pass_through"] = (f.nonroutine_dr / f.ext_cr.clip(lower=1000)).clip(upper=3)
        f["f_max_credit_vs_expected"] = f.max_cr / exp_cr
        # dormancy: days between previous customer activity and the first txn of the month
        last_pre = pd.to_datetime(f.account_id.map(e.acct.last_activity_pre_window))
        prev_last = t.groupby(["account_id", "month"]).txn_ts.max().groupby(level=0).shift(1).reset_index(drop=True)
        f["f_dormancy_days"] = (f.first_ts - prev_last.fillna(last_pre)).dt.days.clip(lower=0)
        return f

    def card_features(self):
        e, t = self.e, self.tx[self.tx.product_type == "CARD"].copy()
        for col, mask in {"purch": t.txn_type == "PURCHASE", "cadv": t.txn_type == "CASH_ADVANCE",
                          "pay": t.txn_type == "PAYMENT", "cpay": (t.txn_type == "PAYMENT") & t.is_cash,
                          "tpay": t.is_third_party & (t.txn_type == "PAYMENT"),
                          "hrm": (t.txn_type == "PURCHASE") & t.mcc.isin(C.HIGH_RISK_MCC),
                          "cbr": t.txn_type == "CREDIT_BALANCE_REFUND",
                          "ret": t.txn_type == "RETURNED_PAYMENT"}.items():
            t[col] = t.amount.where(mask, 0)
        t["n_cadv"] = (t.txn_type == "CASH_ADVANCE").astype(int)
        g = t.groupby(["account_id", "month"])
        f = g.agg(purch=("purch", "sum"), f_cash_adv_amt=("cadv", "sum"), f_n_cash_adv=("n_cadv", "sum"),
                  pay=("pay", "sum"), f_cash_payment_amt=("cpay", "sum"), f_third_party_pay_amt=("tpay", "sum"),
                  f_hr_mcc_amt=("hrm", "sum"), f_cb_refund_amt=("cbr", "sum"), f_returned_pay_amt=("ret", "sum"))
        tp = t[t.is_third_party & (t.txn_type == "PAYMENT")]
        f["f_n_third_party_payors"] = tp.groupby(["account_id", "month"]).counterparty_id.nunique()
        f = f.fillna(0).reset_index()
        lim = f.account_id.map(e.acct.credit_limit).clip(lower=500)
        f["f_purchase_vs_limit"] = f.purch / lim
        f["f_payments_vs_limit"] = f.pay / lim
        f["f_hr_mcc_share"] = f.f_hr_mcc_amt / f.purch.clip(lower=1)
        usual = f.groupby("account_id").purch.transform("median").clip(lower=50)
        f["f_spend_vs_usual"] = f.purch / usual
        return f

    def loan_features(self):
        e, t = self.e, self.tx[self.tx.product_type == "LOAN"].copy()
        t["paid"] = t.amount.where(t.direction == "CR", 0)
        t["extra"] = t.amount.where(t.txn_type.isin(["EXTRA_PAYMENT", "PAYOFF"]), 0)
        t["tp"] = (t.is_third_party & (t.direction == "CR")).astype(int)
        t["cashp"] = t.amount.where((t.direction == "CR") & t.is_cash, 0)
        g = t.groupby(["account_id", "month"])
        f = g.agg(paid=("paid", "sum"), extra=("extra", "sum"), f_n_third_party_pay=("tp", "sum"),
                  f_cash_pay_amt=("cashp", "sum"), last_ts=("txn_ts", "max")).reset_index()
        a = e.acct
        f["f_payments_vs_scheduled"] = f.paid / f.account_id.map(a.monthly_payment).clip(lower=50)
        f["f_extra_vs_loan"] = f.extra / f.account_id.map(a.loan_amount)
        f["f_months_since_open"] = (f.last_ts - pd.to_datetime(f.account_id.map(a.open_date))).dt.days / 30.4
        # proceeds moved out within 7 days of a disbursement into an own deposit account
        dis = self.tx[self.tx.txn_type == "LOAN_DISBURSEMENT_IN"]
        dep_out = self.tx[(self.tx.product_type == "DEPOSIT") & self.tx.txn_type.isin(
            ["WIRE_OUT", "P2P_OUT", "CHECK_PAID", "ATM_WITHDRAWAL"])]
        ratio = {}
        for _, d in dis.iterrows():
            o = dep_out[(dep_out.account_id == d.account_id) & (dep_out.txn_ts > d.txn_ts)
                        & (dep_out.txn_ts <= d.txn_ts + pd.Timedelta(days=7))]
            ratio[(d.counter_account_id, d.month)] = o.amount.sum() / d.amount
        f["f_disb_outflow_ratio"] = [ratio.get((a_, m), 0.0) for a_, m in zip(f.account_id, f.month)]
        return f

    # -------------------------------------------------------------- model
    def run_product(self, product, f):
        model_id, model_name = MODELS[product]
        feats = list(FEATURES[product])
        X = f[feats].astype(float).copy()
        for c in feats:
            if c in LOG_FEATURES:
                X[c] = np.log1p(X[c])            # tame heavy-tailed money amounts
        X = X.replace([np.inf, -np.inf], 0).fillna(0)
        model = IsolationForest(n_estimators=300, random_state=C.SEED, n_jobs=-1).fit(X)
        f["anomaly_score"] = -model.score_samples(X)        # higher = more anomalous
        f["anomaly_pct"] = f.anomaly_score.rank(pct=True) * 100
        thr_score = float(np.percentile(f.anomaly_score, C.ML_EVENT_PERCENTILE))
        flagged = f[f.anomaly_score >= thr_score].copy()

        # SHAP for IsolationForest explains the path length: NEGATIVE values push towards "anomalous"
        sv = shap.TreeExplainer(model).shap_values(X.loc[flagged.index])
        medians = f[feats].median()
        tx_by = dict(tuple(self.tx[self.tx.product_type == product].groupby(["account_id", "month"])))

        for row_i, (idx, row) in enumerate(flagged.iterrows()):
            contrib = pd.Series(-sv[row_i], index=feats)          # flip sign: positive = more anomalous
            top = contrib[contrib > 0].sort_values(ascending=False).head(3)
            reasons, masks = [], None
            mtx = tx_by[(row.account_id, row.month)]
            for feat in top.index:
                label, kind, filt = FEATURES[product][feat]
                reasons.append(f"{label}: {fmt(row[feat], kind)} (typical {fmt(medians[feat], kind)})")
                m = filt(mtx)
                masks = m if masks is None else (masks | m)
            trig = mtx[masks] if masks is not None and masks.any() else mtx.nlargest(5, "amount")
            if len(trig) > 40:                       # keep the drill-down readable
                trig = trig.nlargest(40, "amount")
            pts = C.ML_POINTS_MIN + (C.ML_POINTS_MAX - C.ML_POINTS_MIN) * \
                min(1.0, (row.anomaly_pct - C.ML_EVENT_PERCENTILE) / (100 - C.ML_EVENT_PERCENTILE))
            self.sink.add("ML", model_id, row.account_id, mtx.txn_ts.max(),
                          f"Anomaly score {row.anomaly_score:.3f} - top {100 - row.anomaly_pct:.2f}% of "
                          f"{product.lower()} account-months in {row.month}",
                          f"Top {100 - C.ML_EVENT_PERCENTILE:.0f}% (score >= {thr_score:.3f})",
                          row.anomaly_score / thr_score, trig, base_points=round(pts, 1),
                          reason_codes=" | ".join(reasons), model_score=round(float(row.anomaly_score), 4),
                          window_start=mtx.txn_ts.min())
        self.catalog.append(dict(model_id=model_id, model_name=model_name, version="1.0",
                                 model_type="IsolationForest (unsupervised)", product_scope=product,
                                 features=", ".join(feats), event_rule=f"anomaly percentile >= {C.ML_EVENT_PERCENTILE}",
                                 score_threshold=round(thr_score, 4), n_training_rows=len(f),
                                 n_events=len(flagged)))
        out = f[["account_id", "month", "anomaly_score", "anomaly_pct"] + feats].copy()
        out.insert(0, "model_id", model_id)
        self.features_out.append(out)

    def run(self):
        self.run_product("DEPOSIT", self.deposit_features())
        self.run_product("CARD", self.card_features())
        self.run_product("LOAN", self.loan_features())
        self.tx.drop(columns="is_third_party", inplace=True)
        return self.sink


if __name__ == "__main__":
    import time
    from rules_engine import RulesEngine
    t0 = time.time()
    b = MLEventBuilder(RulesEngine())
    ev, lk = b.run().frames()
    print(ev.groupby("source_id").size().to_string(), f"\n{len(ev)} ML events in {time.time() - t0:.0f}s")
    print(ev.reason_codes.head(5).to_string())
