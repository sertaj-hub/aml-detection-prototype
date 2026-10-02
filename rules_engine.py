"""
rules_engine.py - Deterministic AML scenarios (rules) over the synthetic data.

Each rule hit becomes a DETECTION EVENT tied to (account, primary party), with:
  - observed value vs threshold in plain English (for the investigator)
  - base points x severity (how far above threshold) = event points
  - the exact transactions that triggered it (event_transactions, many-to-many)

Monitoring cycle = calendar month. Rolling-window rules (structuring, rapid in/out, dormant,
cross-product) fire in the month of the last triggering transaction.

Java analogy: each rule is like an implementation of `interface Rule { List<Event> evaluate(Context c); }`.
Here each rule is a method; RULES maps rule_id -> metadata (the rule catalog).
"""
from datetime import timedelta
import numpy as np
import pandas as pd

import config as C

RULES = [
    # rule_id, name, product, typology, logic, base_points
    ("R-DEP-01", "Structuring", "DEPOSIT", "STRUCT",
     ">=3 cash deposits of $8,000-$9,999 into one account within 7 days", 30),
    ("R-DEP-02", "Rapid movement of funds", "DEPOSIT", "RAPID_MOVE",
     "External credits >= $20,000 within 3 days, and >= 80% debited out within the following 3 days", 25),
    ("R-DEP-03", "High-risk geography wire", "DEPOSIT", "HR_WIRE",
     "Any wire >= $5,000 with a high-risk country, or >= 3 high-risk-country wires in a month", 20),
    ("R-DEP-04", "Dormant account reactivation", "DEPOSIT", "DORMANT",
     "No customer activity for >= 180 days, then >= $10,000 of credits within 30 days of reactivation", 15),
    ("R-DEP-05", "Cash inconsistent with profile", "DEPOSIT", "CASH_PROFILE",
     "Monthly cash deposits >= $10,000 and >= 3x the party's expected monthly cash", 20),
    ("R-DEP-06", "Funnel / many-to-one", "DEPOSIT", "FUNNEL",
     ">= 6 inbound credits from >= 4 distinct sources in a month totalling >= $8,000, with >= 70% moved out", 30),
    ("R-CRD-01", "Cash-advance cycling", "CARD", "CC_CASH_ADV",
     ">= 3 cash advances totalling >= $2,500 in a month, repaid (>= 80%) in the same month", 20),
    ("R-CRD-02", "Overpayment and credit-balance refund", "CARD", "CC_OVERPAY_REFUND",
     "Credit-balance refund >= $1,500 issued after card overpayment", 22),
    ("R-CRD-03", "Third-party card payments", "CARD", "CC_3P_PAY",
     "Card payments from >= 2 distinct third parties totalling >= $2,000 in a month", 15),
    ("R-CRD-04", "High-risk MCC spend", "CARD", "CC_HRMCC",
     "Gambling / crypto (MCC 7995, 6051) spend >= $3,000 and >= 30% of card spend in a month", 15),
    ("R-CRD-05", "Bust-out pattern", "CARD", "CC_BUSTOUT",
     "Returned card payment, or 14-day spend >= 85% of limit and >= 3x usual monthly spend", 20),
    ("R-LN-01", "Early loan payoff / lump sum", "LOAN", "LN_EARLY_PAYOFF",
     "Payoff or extra payment >= $10,000 (or >= 25% of loan amount) within 24 months of origination", 18),
    ("R-LN-02", "Loan proceeds moved out rapidly", "LOAN", "LN_RAPID_OUT",
     "Loan disbursed to own account and >= 70% debited out within 7 days", 25),
    ("R-LN-03", "Third-party / cash loan repayments", "LOAN", "LN_3P_REPAY",
     ">= 2 loan payments in a month from third parties or in cash", 12),
    ("R-XP-01", "Cash-funded credit paydown (cross-product)", "CROSS_PRODUCT", "XP_CASH_TO_CREDIT",
     "Party cash-in >= $10,000 (and >= 2x expected cash) within 21 days, with >= 50% used to pay down cards/loans", 25),
]
RULE_CATALOG = pd.DataFrame(RULES, columns=["rule_id", "rule_name", "product_scope", "typology",
                                            "logic", "base_points"])
RULE_CATALOG["version"] = "1.0"
POINTS = dict(zip(RULE_CATALOG.rule_id, RULE_CATALOG.base_points))
NAMES = dict(zip(RULE_CATALOG.rule_id, RULE_CATALOG.rule_name))

CREDIT_TYPES_EXTERNAL = {"WIRE_IN", "ACH_CREDIT", "CHECK_DEPOSIT", "CASH_DEPOSIT", "P2P_IN"}


def money(x):
    return f"${x:,.0f}"


def severity(ratio):
    """1.0 at threshold, rising to 1.5 at 3x threshold."""
    return float(min(1.5, max(1.0, 1 + 0.25 * (ratio - 1))))


class EventSink:
    """Collects events + their transactions. Shared by the rules engine and the ML layer."""

    def __init__(self, acct_party, acct_product, prefix="RE"):
        self.events, self.links = [], []
        self.prefix = prefix
        self.acct_party, self.acct_product = acct_party, acct_product
        self.n = 0

    def add(self, event_type, source_id, account_id, fire_ts, observed, threshold, ratio,
            trigger_txns, context_txns=None, base_points=None, reason_codes=None, model_score=None,
            window_start=None, product_type=None):
        """trigger_txns: DataFrame with txn_id, amount (contribution is split by amount)."""
        self.n += 1
        eid = f"{self.prefix}{self.n:06d}"
        sev = severity(ratio) if event_type == "RULE" else 1.0
        pts = round((base_points if base_points is not None else POINTS[source_id]) * sev, 1)
        self.events.append(dict(
            event_id=eid, event_type=event_type, source_id=source_id,
            source_name=NAMES.get(source_id, source_id), account_id=account_id,
            primary_party_id=self.acct_party[account_id],
            product_type=product_type or self.acct_product[account_id],
            fire_ts=fire_ts, cycle_month=fire_ts.strftime("%Y-%m"),
            window_start=window_start if window_start is not None else trigger_txns.txn_ts.min(),
            observed=observed, threshold=threshold, ratio_to_threshold=round(float(ratio), 2),
            severity=round(sev, 2), points=pts, model_score=model_score, reason_codes=reason_codes))
        tot = trigger_txns.amount.sum()
        for tid, amt in zip(trigger_txns.txn_id, trigger_txns.amount):
            self.links.append((eid, tid, "TRIGGER", round(pts * amt / tot, 2) if tot else 0.0))
        if context_txns is not None:
            for tid in context_txns.txn_id:
                self.links.append((eid, tid, "CONTEXT", 0.0))
        return eid

    def frames(self):
        ev = pd.DataFrame(self.events)
        lk = pd.DataFrame(self.links, columns=["event_id", "txn_id", "link_role", "points_contribution"])
        return ev, lk.drop_duplicates(["event_id", "txn_id"])


def greedy_windows(df, days, cond):
    """Slide over time-sorted rows; when rows within `days` of row i satisfy cond(group), emit the
    group and jump past it (non-overlapping episodes). Java: classic two-pointer sliding window."""
    out, ts = [], df.txn_ts.values
    i, n = 0, len(df)
    while i < n:
        j = np.searchsorted(ts, ts[i] + np.timedelta64(days, "D"), side="right")
        g = df.iloc[i:j]
        if cond(g):
            out.append(g)
            i = j
        else:
            i += 1
    return out


class RulesEngine:
    def __init__(self, data_dir="data"):
        rd = lambda n: pd.read_parquet(f"{data_dir}/{n}.parquet")
        self.tx = rd("transactions")
        self.accounts = rd("accounts")
        self.parties = rd("parties").set_index("party_id")
        self.roles = rd("party_account_role")
        self.cps = rd("counterparties").set_index("counterparty_id")
        prim = self.roles[self.roles.role == "PRIMARY"]
        self.acct_party = dict(zip(prim.account_id, prim.party_id))
        self.acct_product = dict(zip(self.accounts.account_id, self.accounts.product_type))
        self.acct = self.accounts.set_index("account_id")
        self.tx["month"] = self.tx.txn_ts.dt.strftime("%Y-%m")
        self.tx["primary_party_id"] = self.tx.account_id.map(self.acct_party)
        # parties on each account (any role) - used to tell "own money" from third-party money
        self.acct_members = self.roles.groupby("account_id").party_id.apply(set).to_dict()
        self.cp_type = self.cps.cp_type.to_dict()
        self.cp_link = self.cps.linked_party_id.to_dict()
        self.sink = EventSink(self.acct_party, self.acct_product)

    def third_party_mask(self, df):
        """Vectorised: external money not from a party on the account (nor their own external account)."""
        linked = df.counterparty_id.map(self.cp_link)
        members = df.account_id.map(self.acct_members)
        own = [l in m if isinstance(m, set) else False for l, m in zip(linked, members)]
        return df.counterparty_id.notna() & ~pd.Series(own, index=df.index)

    # ================================================================ DEPOSIT
    def r_dep_01(self):
        q = self.tx[(self.tx.txn_type == "CASH_DEPOSIT") & self.tx.amount.between(8000, 9999.99)
                    & (self.tx.product_type == "DEPOSIT")]
        for acct, g in q.groupby("account_id"):
            for w in greedy_windows(g, 7, lambda x: len(x) >= 3):
                days = (w.txn_ts.max() - w.txn_ts.min()).days + 1
                self.sink.add("RULE", "R-DEP-01", acct, w.txn_ts.max(),
                              f"{len(w)} cash deposits {money(w.amount.min())}-{money(w.amount.max())} in {days} days "
                              f"(total {money(w.amount.sum())})", RULES[0][4], len(w) / 3, w)

    def r_dep_02(self):
        t = self.tx[self.tx.product_type == "DEPOSIT"]
        cr = t[t.txn_type.isin(CREDIT_TYPES_EXTERNAL) & (t.amount >= 1000)]
        dr = t[(t.direction == "DR") & (t.amount >= 500)]
        dr_by = dict(tuple(dr.groupby("account_id")))
        for acct, g in cr.groupby("account_id"):
            if acct not in dr_by:
                continue
            for w in greedy_windows(g, 3, lambda x: x.amount.sum() >= 20000):
                d = dr_by[acct]
                outs = d[(d.txn_ts >= w.txn_ts.min()) & (d.txn_ts <= w.txn_ts.max() + timedelta(days=3))]
                ratio = outs.amount.sum() / w.amount.sum()
                if ratio >= 0.8:
                    trig = pd.concat([w, outs])
                    self.sink.add("RULE", "R-DEP-02", acct, trig.txn_ts.max(),
                                  f"{money(w.amount.sum())} in, {money(outs.amount.sum())} ({ratio:.0%}) out within "
                                  f"{(trig.txn_ts.max() - w.txn_ts.min()).days + 1} days", RULES[1][4],
                                  min(w.amount.sum() / 20000, ratio / 0.8 * 2), trig)

    def r_dep_03(self):
        q = self.tx[self.tx.txn_type.isin(["WIRE_IN", "WIRE_OUT"]) & self.tx.cp_country.isin(C.HIGH_RISK_COUNTRIES)]
        for (acct, m), g in q.groupby(["account_id", "month"]):
            big = g[g.amount >= 5000]
            if len(big) or len(g) >= 3:
                ctry = ", ".join(sorted(g.cp_country.unique()))
                self.sink.add("RULE", "R-DEP-03", acct, g.txn_ts.max(),
                              f"{len(g)} wire(s) totalling {money(g.amount.sum())} with {ctry} "
                              f"(largest {money(g.amount.max())})", RULES[2][4],
                              max(g.amount.max() / 5000, len(g) / 3), g)

    def r_dep_04(self):
        t = self.tx[(self.tx.product_type == "DEPOSIT") & (self.tx.channel != "SYSTEM")]
        last_pre = pd.to_datetime(t.account_id.map(self.acct.last_activity_pre_window))
        # previous customer activity on the same account (vectorised: groupby + shift)
        prev = t.groupby("account_id").txn_ts.shift(1).fillna(last_pre)
        gap = (t.txn_ts - prev).dt.days
        wakes = t[gap >= 180].groupby("account_id").head(1)
        for _, w0 in wakes.iterrows():
            g = t[t.account_id == w0.account_id]
            w = g[(g.txn_ts >= w0.txn_ts) & (g.txn_ts <= w0.txn_ts + timedelta(days=30))]
            credits = w[w.direction == "CR"]
            if credits.amount.sum() >= 10000:
                self.sink.add("RULE", "R-DEP-04", w0.account_id, w.txn_ts.max(),
                              f"Dormant {gap[w0.name]} days, then {money(credits.amount.sum())} credited and "
                              f"{money(w[w.direction == 'DR'].amount.sum())} debited within 30 days",
                              RULES[3][4], credits.amount.sum() / 10000, w)

    def r_dep_05(self):
        q = self.tx[(self.tx.txn_type == "CASH_DEPOSIT") & (self.tx.product_type == "DEPOSIT")]
        exp = self.parties.expected_monthly_cash
        for (acct, m), g in q.groupby(["account_id", "month"]):
            tot = g.amount.sum()
            e = max(float(exp[self.acct_party[acct]]), 100.0)
            if tot >= 10000 and tot >= 3 * e:
                self.sink.add("RULE", "R-DEP-05", acct, g.txn_ts.max(),
                              f"{money(tot)} cash in {m} vs expected {money(e)}/month ({tot / e:.1f}x)",
                              RULES[4][4], min(tot / 10000, tot / (3 * e)), g)

    def r_dep_06(self):
        t = self.tx[self.tx.product_type == "DEPOSIT"].copy()
        home = self.acct.branch_state
        inbound = t[(t.direction == "CR") & (
            t.txn_type.isin(["P2P_IN", "ACH_CREDIT"]) & t.counterparty_id.map(self.cp_type).eq("INDIVIDUAL")
            | ((t.txn_type == "P2P_IN") & t.counter_account_id.notna())
            | ((t.txn_type == "CASH_DEPOSIT") & (t.counterparty_id.notna() | (t.location_state != t.account_id.map(home))))
        )].copy()
        inbound["source"] = inbound.counterparty_id.fillna(inbound.counter_account_id).fillna(
            "CASH@" + inbound.location_state.astype(str))
        # money leaving the customer's control: excludes routine spend and transfers to their own accounts
        outs = t[(t.direction == "DR") & ~t.txn_type.isin(["POS_PURCHASE", "ACH_DEBIT", "INTEREST", "TRANSFER_OUT",
                                                             "CARD_PAYMENT_OUT", "LOAN_PAYMENT_OUT"])]
        outs_by = dict(tuple(outs.groupby(["account_id", "month"])))
        for (acct, m), g in inbound.groupby(["account_id", "month"]):
            n_src, tot = g.source.nunique(), g.amount.sum()
            if len(g) >= 6 and n_src >= 4 and tot >= 8000:
                o = outs_by.get((acct, m))
                if o is None:
                    continue
                ratio = o.amount.sum() / tot
                if ratio >= 0.7:
                    self.sink.add("RULE", "R-DEP-06", acct, max(g.txn_ts.max(), o.txn_ts.max()),
                                  f"{len(g)} inbound credits from {n_src} sources ({money(tot)}), "
                                  f"{ratio:.0%} moved out in {m}", RULES[5][4],
                                  min(n_src / 4, tot / 8000), pd.concat([g, o]))

    # ================================================================== CARDS
    def r_crd_01(self):
        c = self.tx[self.tx.product_type == "CARD"]
        ca = c[c.txn_type == "CASH_ADVANCE"]
        pays = dict(tuple(c[c.txn_type == "PAYMENT"].groupby(["account_id", "month"])))
        for (acct, m), g in ca.groupby(["account_id", "month"]):
            tot = g.amount.sum()
            if len(g) >= 3 and tot >= 2500:
                p = pays.get((acct, m))
                if p is not None and p.amount.sum() >= 0.8 * tot:
                    self.sink.add("RULE", "R-CRD-01", acct, max(g.txn_ts.max(), p.txn_ts.max()),
                                  f"{len(g)} cash advances {money(tot)}, repaid {money(p.amount.sum())} in {m} "
                                  f"({p.is_cash.sum()} cash payments)", RULES[6][4], tot / 2500, pd.concat([g, p]))

    def r_crd_02(self):
        c = self.tx[self.tx.product_type == "CARD"]
        ref = c[(c.txn_type == "CREDIT_BALANCE_REFUND") & (c.amount >= 1500)]
        pays = c[c.txn_type == "PAYMENT"]
        for _, row in ref.iterrows():
            p = pays[(pays.account_id == row.account_id) & (pays.txn_ts <= row.txn_ts)
                     & (pays.txn_ts >= row.txn_ts - timedelta(days=30))]
            trig = pd.concat([row.to_frame().T, p])
            trig["amount"] = trig.amount.astype(float)
            self.sink.add("RULE", "R-CRD-02", row.account_id, row.txn_ts,
                          f"Credit-balance refund {money(row.amount)} after {len(p)} payment(s) of "
                          f"{money(p.amount.sum())} in prior 30 days", RULES[7][4], row.amount / 1500, trig)

    def r_crd_03(self):
        c = self.tx[(self.tx.product_type == "CARD") & (self.tx.txn_type == "PAYMENT") & self.tx.counterparty_id.notna()]
        c = c[self.third_party_mask(c)]
        for (acct, m), g in c.groupby(["account_id", "month"]):
            n = g.counterparty_id.nunique()
            if n >= 2 and g.amount.sum() >= 2000:
                self.sink.add("RULE", "R-CRD-03", acct, g.txn_ts.max(),
                              f"{len(g)} payments from {n} third parties totalling {money(g.amount.sum())} in {m}",
                              RULES[8][4], max(n / 2, g.amount.sum() / 2000), g)

    def r_crd_04(self):
        c = self.tx[(self.tx.product_type == "CARD") & (self.tx.txn_type == "PURCHASE")]
        spend = c.groupby(["account_id", "month"]).amount.sum()
        hr = c[c.mcc.isin(C.HIGH_RISK_MCC)]
        for (acct, m), g in hr.groupby(["account_id", "month"]):
            tot, share = g.amount.sum(), g.amount.sum() / spend[(acct, m)]
            if tot >= 3000 and share >= 0.3:
                self.sink.add("RULE", "R-CRD-04", acct, g.txn_ts.max(),
                              f"{money(tot)} gambling/crypto spend in {m} ({share:.0%} of card spend)",
                              RULES[9][4], tot / 3000, g)

    def r_crd_05(self):
        c = self.tx[self.tx.product_type == "CARD"]
        lim = self.acct.credit_limit
        for _, row in c[c.txn_type == "RETURNED_PAYMENT"].iterrows():
            w = c[(c.account_id == row.account_id) & (c.txn_ts >= row.txn_ts - timedelta(days=30))
                  & (c.txn_ts <= row.txn_ts) & c.txn_type.isin(["PURCHASE", "CASH_ADVANCE", "RETURNED_PAYMENT", "PAYMENT"])]
            self.sink.add("RULE", "R-CRD-05", row.account_id, row.txn_ts,
                          f"Payment of {money(row.amount)} returned; {money(w[w.txn_type != 'RETURNED_PAYMENT'].query('direction==\"DR\"').amount.sum())} "
                          f"drawn in prior 30 days on a {money(lim[row.account_id])} limit", RULES[10][4], 2.0, w)
        flagged = set(c[c.txn_type == "RETURNED_PAYMENT"].account_id)
        dr = c[c.txn_type.isin(["PURCHASE", "CASH_ADVANCE"])]
        monthly = dr.groupby(["account_id", "month"]).amount.sum()
        roll = (dr.set_index("txn_ts").groupby("account_id").amount.rolling("14D").sum()
                  .groupby(level=0).max())
        cand = set(roll[roll >= 0.85 * roll.index.map(lim)].index) - flagged
        for acct, g in dr[dr.account_id.isin(cand)].groupby("account_id"):
            for w in greedy_windows(g, 14, lambda x: x.amount.sum() >= 0.85 * lim[acct]):
                tot = w.amount.sum()
                others = monthly[acct].drop(w.month.iloc[-1], errors="ignore")
                usual = others.median() if len(others) else 0
                if tot >= 3 * max(usual, 1):
                    self.sink.add("RULE", "R-CRD-05", acct, w.txn_ts.max(),
                                  f"{money(tot)} drawn in 14 days = {tot / lim[acct]:.0%} of {money(lim[acct])} limit "
                                  f"(usual {money(usual)}/month)", RULES[10][4], tot / (0.85 * lim[acct]), w)

    # ================================================================== LOANS
    def r_ln_01(self):
        l = self.tx[(self.tx.product_type == "LOAN") & self.tx.txn_type.isin(["PAYOFF", "EXTRA_PAYMENT"])]
        dep = self.tx[(self.tx.product_type == "DEPOSIT") & (self.tx.direction == "CR")
                      & (self.tx.channel != "INTERNAL") & (self.tx.amount >= 2000)]
        dep_by_party = dict(tuple(dep.groupby("primary_party_id")))
        for _, row in l.iterrows():
            a = self.acct.loc[row.account_id]
            age_m = (row.txn_ts.date() - a.open_date).days / 30.4
            if age_m <= 24 and (row.amount >= 10000 or row.amount >= 0.25 * a.loan_amount):
                d = dep_by_party.get(row.primary_party_id)
                ctx = None
                if d is not None:
                    ctx = d[(d.txn_ts <= row.txn_ts) & (d.txn_ts >= row.txn_ts - timedelta(days=14))]
                src = "funded from: " + (", ".join(sorted(ctx.txn_type.unique())) if ctx is not None and len(ctx)
                                         else ("third party" if self.third_party_mask(row.to_frame().T).iloc[0] else "own funds"))
                trig = row.to_frame().T
                trig["amount"] = trig.amount.astype(float)
                self.sink.add("RULE", "R-LN-01", row.account_id, row.txn_ts,
                              f"{row.txn_type.title().replace('_', ' ')} {money(row.amount)} on "
                              f"{money(a.loan_amount)} loan, {age_m:.0f} months after origination; {src}",
                              RULES[11][4], max(row.amount / 10000, row.amount / (0.25 * a.loan_amount)),
                              trig, context_txns=ctx)

    def r_ln_02(self):
        dis = self.tx[self.tx.txn_type == "LOAN_DISBURSEMENT_IN"]
        dep = self.tx[(self.tx.product_type == "DEPOSIT") & (self.tx.direction == "DR")
                      & self.tx.txn_type.isin(["WIRE_OUT", "P2P_OUT", "CHECK_PAID", "ATM_WITHDRAWAL"])]
        for _, row in dis.iterrows():
            o = dep[(dep.account_id == row.account_id) & (dep.txn_ts > row.txn_ts)
                    & (dep.txn_ts <= row.txn_ts + timedelta(days=7))]
            ratio = o.amount.sum() / row.amount
            if ratio >= 0.7:
                loan = row.counter_account_id
                intl = (o.cp_country != "US").any()
                trig = pd.concat([self.tx[(self.tx.account_id == loan) & (self.tx.txn_type == "DISBURSEMENT")], o])
                self.sink.add("RULE", "R-LN-02", loan, o.txn_ts.max(),
                              f"{money(row.amount)} loan proceeds; {money(o.amount.sum())} ({ratio:.0%}) moved out "
                              f"within 7 days{' incl. international' if intl else ''}",
                              RULES[12][4], ratio / 0.7 * (1.3 if intl else 1.0), trig)

    def r_ln_03(self):
        l = self.tx[(self.tx.product_type == "LOAN") & self.tx.txn_type.isin(["PAYMENT", "EXTRA_PAYMENT"])]
        l = l[l.is_cash | self.third_party_mask(l)]
        for (acct, m), g in l.groupby(["account_id", "month"]):
            if len(g) >= 2:
                self.sink.add("RULE", "R-LN-03", acct, g.txn_ts.max(),
                              f"{len(g)} loan payments in {m} from third parties / cash ({money(g.amount.sum())})",
                              RULES[13][4], len(g) / 2, g)

    # ========================================================= CROSS-PRODUCT
    def r_xp_01(self):
        t = self.tx
        cash_in = t[t.is_cash & (t.direction == "CR")
                    & t.txn_type.isin(["CASH_DEPOSIT", "PAYMENT", "EXTRA_PAYMENT", "PAYOFF"])]
        paydown = t[((t.product_type == "CARD") & (t.txn_type == "PAYMENT"))
                    | ((t.product_type == "LOAN") & t.txn_type.isin(["EXTRA_PAYMENT", "PAYOFF"]))]
        pay_by = dict(tuple(paydown.groupby("primary_party_id")))
        exp = self.parties.expected_monthly_cash
        for pid, g in cash_in.groupby("primary_party_id"):
            if pid not in pay_by:
                continue
            e = max(float(exp[pid]), 100.0)
            for w in greedy_windows(g, 21, lambda x: x.amount.sum() >= max(10000, 2 * e)):
                p = pay_by[pid]
                p = p[(p.txn_ts >= w.txn_ts.min()) & (p.txn_ts <= w.txn_ts.max() + timedelta(days=7))]
                cash, pd_amt = w.amount.sum(), p.amount.sum()
                if pd_amt >= 0.5 * cash:
                    target = p.groupby("account_id").amount.sum().idxmax()   # attribute to most-paid-down account
                    trig = pd.concat([w, p]).drop_duplicates("txn_id")
                    prods = sorted(set(trig.product_type))
                    self.sink.add("RULE", "R-XP-01", target, trig.txn_ts.max(),
                                  f"{money(cash)} cash in over {(w.txn_ts.max() - w.txn_ts.min()).days + 1} days "
                                  f"across {w.account_id.nunique()} account(s); {money(pd_amt)} paid down on credit "
                                  f"({'/'.join(prods)})", RULES[14][4], min(cash / 10000, pd_amt / cash * 2), trig,
                                  product_type="CROSS_PRODUCT")

    # ------------------------------------------------------------------ run
    def run(self):
        for rid in RULE_CATALOG.rule_id:
            getattr(self, rid.lower().replace("-", "_"))()   # "R-DEP-01" -> self.r_dep_01()
        return self.sink


if __name__ == "__main__":
    import time
    t0 = time.time()
    eng = RulesEngine()
    sink = eng.run()
    ev, lk = sink.frames()
    RULE_CATALOG.to_parquet("data/rule_catalog.parquet", index=False)
    ev.to_parquet("data/rule_events.parquet", index=False)
    lk.to_parquet("data/rule_event_transactions.parquet", index=False)
    print(ev.groupby("source_id").size().to_string())
    print(f"{len(ev):,} rule events, {len(lk):,} event-txn links in {time.time() - t0:.0f}s")
