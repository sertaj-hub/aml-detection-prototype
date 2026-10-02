"""
generate_data.py - Synthetic retail-banking AML dataset (Apr-Sep 2026).

Builds parties, accounts, party-account roles, counterparties and transactions across
deposits, credit cards and loans, then injects labelled money-laundering typologies
and legitimate "noisy" spikes that look suspicious but aren't.

Outputs (data/): parties, accounts, party_account_role, counterparties, transactions
(+ deposit_txns, card_txns, loan_ledger views), labels_transactions, labels_accounts,
labels_parties, schemes.

Conventions (account perspective):
  deposit : CR = money in,              DR = money out
  card    : DR = increases balance owed (purchase, cash advance, credit-balance refund),
            CR = reduces it (payment, merchant refund)
  loan    : DR = disbursement,           CR = repayment
"""
from datetime import datetime, timedelta, date
import math
import numpy as np
import pandas as pd
from faker import Faker

import config as C
from common import IdGen, TxnBuffer, month_bounds, ts_on, rand_day, amortized_payment


class SyntheticBank:
    def __init__(self, seed=C.SEED):
        # Java: new Random(seed). numpy's Generator is the modern, faster RNG API.
        self.rng = np.random.default_rng(seed)
        self.fake = Faker("en_US")
        Faker.seed(seed)

        self.parties, self.accounts, self.roles, self.cps = [], [], [], []
        self.acct = {}                 # account_id -> account dict   (Java: HashMap<String, Account>)
        self.party = {}                # party_id -> party dict
        self.primary_accts = {}        # party_id -> {subtype: [account_id, ...]}
        self.acct_parties = {}         # account_id -> {party_id: role}
        self.self_ext_cp = {}          # party_id -> counterparty_id of their account at another bank

        self.txn = TxnBuffer()
        self.label_rows = []           # (txn_id, scheme_id, typology)
        self.schemes = []
        self.scheme_party_roles = []   # (scheme_id, party_id, role_in_scheme)

        self.ids = {k: IdGen(p, w) for k, p, w in [
            ("party", "P", 6), ("acct", "ACC", 7), ("cp", "CP", 6), ("scheme", "SCH", 4)]}

        # Behavioural constraints decided during planning, respected by the normal generator
        self.dormant_until = {}        # account_id -> datetime (no normal activity before this)
        self.loan_closed_on = {}       # loan account_id -> datetime (no scheduled payments after)
        self.card_stop_on = {}         # card account_id -> datetime (no normal activity after)
        self.spikes = []               # (party_id, spike_type)

    # ------------------------------------------------------------------ utils
    def lognorm(self, mean, sigma):
        """Lognormal draw with the given arithmetic mean."""
        return float(self.rng.lognormal(math.log(mean) - sigma ** 2 / 2, sigma))

    def choice(self, seq, p=None):
        return seq[int(self.rng.choice(len(seq), p=p))]

    def chance(self, p):
        return self.rng.random() < p

    def label(self, txn_id, scheme_id, typology):
        if txn_id is not None:
            self.label_rows.append((txn_id, scheme_id, typology))
        return txn_id

    def add(self, acct_id, ts, txn_type, direction, amount, channel, **kw):
        a = self.acct[acct_id]
        kw.setdefault("location_state", a["branch_state"])
        return self.txn.add(acct_id, a["product_type"], ts, txn_type, direction, amount, channel, **kw)

    def xfer(self, from_acct, to_acct, ts, amount, out_type, in_type, by, desc=""):
        """Internal transfer: writes both legs. 'out' leg is DR and 'in' leg is CR in every product."""
        t1 = self.add(from_acct, ts, out_type, "DR", amount, "INTERNAL", counter_account_id=to_acct,
                      initiated_by_party_id=by, description=desc)
        t2 = self.add(to_acct, ts, in_type, "CR", amount, "INTERNAL", counter_account_id=from_acct,
                      initiated_by_party_id=by, description=desc)
        return [t1, t2]

    def first(self, pid, subtype):
        lst = self.primary_accts.get(pid, {}).get(subtype, [])
        return lst[0] if lst else None

    def loans_of(self, pid):
        d = self.primary_accts.get(pid, {})
        return [a for st in ("PERSONAL_LOAN", "AUTO_LOAN", "MORTGAGE") for a in d.get(st, [])]

    # ------------------------------------------------------- counterparties
    def new_cp(self, name, cp_type, country="US", state=None, mcc=None, linked_party_id=None):
        cid = self.ids["cp"].next()
        self.cps.append(dict(counterparty_id=cid, cp_name=name, cp_type=cp_type, country=country,
                             state=state, mcc=mcc, linked_party_id=linked_party_id))
        return cid

    def build_counterparties(self):
        f, r = self.fake, self.rng
        st = lambda: self.choice(C.US_STATES)
        self.cp_pool = {
            "EMPLOYER": [self.new_cp(f.company(), "EMPLOYER", state=st()) for _ in range(400)],
            "UTILITY": [self.new_cp(f"{f.city()} {self.choice(['Power', 'Water', 'Gas', 'Telecom'])}",
                                    "UTILITY", state=st()) for _ in range(30)],
            "LANDLORD": [self.new_cp(self.choice([f.name(), f.company() + ' Properties']), "LANDLORD",
                                     state=st()) for _ in range(700)],
            "BILLER": [self.new_cp(f"{f.company()} {self.choice(['Insurance', 'Wireless', 'Streaming', 'Gym'])}",
                                   "BILLER", state=st()) for _ in range(60)],
            "INDIVIDUAL": [self.new_cp(f.name(), "INDIVIDUAL", state=st()) for _ in range(7000)],
            "AUTO_DEALER": [self.new_cp(f"{f.last_name()} Motors", "AUTO_DEALER", state=st()) for _ in range(50)],
            "TITLE_CO": [self.new_cp(f"{f.last_name()} Title & Escrow", "TITLE_COMPANY", state=st()) for _ in range(30)],
            "BROKERAGE": [self.new_cp(n, "BROKERAGE") for n in
                          ["Vanguard-like Brokerage", "Fidelity-like Brokerage", "Schwab-like Brokerage",
                           "Robo Advisor Inc"]],
            "LAW_FIRM": [self.new_cp(f"{f.last_name()} & {f.last_name()} LLP", "LAW_FIRM", state=st()) for _ in range(30)],
            "LENDER": [self.new_cp(f"{f.last_name()} Mortgage & Lending", "LENDER", state=st()) for _ in range(20)],
            "CRYPTO": [self.new_cp(n, "CRYPTO_EXCHANGE", country=c) for n, c in
                       [("CoinHarbor Exchange", "US"), ("BitNimbus Ltd", "SC"), ("TetherPoint SG", "SG"),
                        ("ChainVault", "US"), ("Kryptex Global", "SC")]],
            "REMIT": [self.new_cp(f.name(), "INDIVIDUAL", country=self.choice(C.LEGIT_REMITTANCE_COUNTRIES))
                      for _ in range(400)],
            "HR_ENTITY": [self.new_cp(self.choice([f.company() + " Trading", f.name()]), "FOREIGN_ENTITY",
                                      country=self.choice(C.HIGH_RISK_COUNTRIES)) for _ in range(200)],
            "OFFSHORE": [self.new_cp(f.company() + self.choice([" Holdings Ltd", " International SA", " FZE"]),
                                     "FOREIGN_ENTITY", country=self.choice(["KY", "PA", "AE", "HK", "CY", "VG"]))
                         for _ in range(120)],
            "GOV_IRS": [self.new_cp("US Treasury - IRS Tax Refund", "GOVERNMENT")],
            "GOV_SSA": [self.new_cp("Social Security Administration", "GOVERNMENT")],
            "PENSION": [self.new_cp(f"{f.company()} Pension Plan", "PENSION") for _ in range(20)],
        }
        self.merchants = {m: [self.new_cp(f"{f.company()} {desc}", "MERCHANT", mcc=m, state=st())
                              for _ in range(40)] for m, (desc, _, _) in C.MCC.items()}
        self.foreign_merchants = {c: [self.new_cp(f"{f.company()} ({c})", "MERCHANT", country=c, mcc="5812")
                                      for _ in range(15)] for c in ["FR", "IT", "JP", "GB", "MX", "ES"]}

    def cp(self, pool):
        return self.choice(self.cp_pool[pool])

    def self_external(self, pid):
        """The party's own account at another bank (lazy-created)."""
        if pid not in self.self_ext_cp:
            p = self.party[pid]
            self.self_ext_cp[pid] = self.new_cp(f"{p['first_name']} {p['last_name']} (ext bank)",
                                                "SELF_EXTERNAL", state=p["state"], linked_party_id=pid)
        return self.self_ext_cp[pid]

    # --------------------------------------------------------------- parties
    def make_party(self, segment, secondary_only=False):
        f, r = self.fake, self.rng
        share, (lo, hi) = C.SEGMENTS[segment]
        income = float(r.uniform(lo, hi))
        occ = self.choice(C.OCCUPATIONS[segment])
        age = {"STUDENT": (18, 25), "RETIREE": (62, 86)}.get(segment, (25, 62))
        dob = date(2026, 1, 1) - timedelta(days=int(r.integers(age[0] * 365, age[1] * 365)))
        cash_intensive = occ in C.CASH_INTENSIVE_OCCUPATIONS
        kyc = self.choice(["LOW", "MEDIUM", "HIGH"],
                          p=[0.62, 0.30, 0.08] if segment == "SELF_EMPLOYED" else [0.82, 0.14, 0.04])
        onboard_lo = 2019 if segment == "STUDENT" else 2005
        onboard = date(int(r.integers(onboard_lo, 2026)), int(r.integers(1, 13)), int(r.integers(1, 28)))
        pid = self.ids["party"].next()
        p = dict(
            party_id=pid, first_name=f.first_name(), last_name=f.last_name(), dob=dob,
            segment=segment, occupation=occ, annual_income=round(income, -2), kyc_risk=kyc,
            pep_flag=bool(segment == "AFFLUENT" and self.chance(0.03)),
            country="US", state=self.choice(C.US_STATES), onboarding_date=onboard,
            expected_monthly_credits=round(income / 12 * 0.8 * r.uniform(0.9, 1.2), -1),
            expected_monthly_cash=round(r.uniform(4000, 14000) if cash_intensive else r.uniform(0, 500), -1),
            is_cash_intensive=cash_intensive, is_secondary_only=secondary_only,
        )
        self.parties.append(p)
        self.party[pid] = p
        return pid

    def build_parties(self):
        segs = list(C.SEGMENTS)
        shares = [C.SEGMENTS[s][0] for s in segs]
        self.primary_ids = [self.make_party(self.choice(segs, p=shares)) for _ in range(C.N_PRIMARY_PARTIES)]
        n_sec = int(C.N_PRIMARY_PARTIES * C.SECONDARY_ONLY_SHARE)
        self.secondary_ids = [self.make_party(self.choice(["MASS", "MASS_AFFLUENT", "RETIREE", "STUDENT"],
                                                          p=[0.45, 0.3, 0.15, 0.1]), secondary_only=True)
                              for _ in range(n_sec)]

    # -------------------------------------------------------------- accounts
    def add_role(self, pid, acct_id, role, start):
        self.roles.append(dict(party_id=pid, account_id=acct_id, role=role, start_date=start, end_date=None))
        self.acct_parties.setdefault(acct_id, {})[pid] = role

    def add_account(self, pid, subtype, open_date=None, **extra):
        r = self.rng
        p = self.party[pid]
        product = {"CHECKING": "DEPOSIT", "SAVINGS": "DEPOSIT", "CD": "DEPOSIT",
                   "CREDIT_CARD": "CARD"}.get(subtype, "LOAN")
        if open_date is None:
            lo = max(p["onboarding_date"], date(2005, 1, 1))
            span = (date(2026, 3, 31) - lo).days
            open_date = lo + timedelta(days=int(r.integers(0, max(span, 1))))
        aid = self.ids["acct"].next()
        a = dict(account_id=aid, product_type=product, product_subtype=subtype, open_date=open_date,
                 status="ACTIVE", branch_state=p["state"], credit_limit=None, loan_amount=None,
                 loan_term_months=None, interest_rate=None, monthly_payment=None,
                 last_activity_pre_window=date(2026, 3, int(r.integers(1, 31))))
        if subtype == "CREDIT_CARD":
            lim = {"MASS": (2000, 8000), "MASS_AFFLUENT": (8000, 20000), "AFFLUENT": (20000, 50000),
                   "STUDENT": (500, 2000), "RETIREE": (3000, 12000), "SELF_EMPLOYED": (5000, 25000)}[p["segment"]]
            a["credit_limit"] = float(round(r.uniform(*lim), -2))
        if product == "LOAN":
            amt, term, rate = {"PERSONAL_LOAN": ((3000, 30000), (24, 61), (0.09, 0.18)),
                               "AUTO_LOAN": ((15000, 55000), (48, 73), (0.05, 0.09)),
                               "MORTGAGE": ((150000, 700000), (360, 361), (0.055, 0.07))}[subtype]
            a["loan_amount"] = float(round(r.uniform(*amt), -2))
            a["loan_term_months"] = int(r.integers(*term))
            a["interest_rate"] = round(float(r.uniform(*rate)), 4)
            a["monthly_payment"] = round(amortized_payment(a["loan_amount"], a["interest_rate"],
                                                           a["loan_term_months"]), 2)
            # make sure the loan hasn't already matured before the window
            min_open = date(2026, 3, 31) - timedelta(days=30 * (a["loan_term_months"] - 6))
            if open_date < min_open:
                open_date = min_open + timedelta(days=int(r.integers(0, 120)))
                open_date = min(open_date, date(2026, 3, 31))
                a["open_date"] = open_date
        a.update(extra)
        self.accounts.append(a)
        self.acct[aid] = a
        self.primary_accts.setdefault(pid, {}).setdefault(subtype, []).append(aid)
        self.add_role(pid, aid, "PRIMARY", a["open_date"])
        return aid

    def pick_secondary(self, exclude):
        pool = self.secondary_ids if self.chance(0.6) else self.primary_ids
        while True:
            s = self.choice(pool)
            if s != exclude:
                return s

    def build_accounts(self):
        r = self.rng
        for pid in self.primary_ids:
            seg = self.party[pid]["segment"]
            has_dep, has_card, has_loan = (self.chance(C.P_DEPOSIT), self.chance(C.P_CARD),
                                           self.chance(C.P_LOAN * (0.4 if seg == "STUDENT" else 1)))
            if not (has_dep or has_card or has_loan):
                has_dep = True
            if has_dep:
                self.add_account(pid, "CHECKING")
                if self.chance(C.P_SAVINGS_GIVEN_DEPOSIT):
                    self.add_account(pid, "SAVINGS")
                if self.chance(C.P_CD_GIVEN_DEPOSIT) and seg != "STUDENT":
                    self.add_account(pid, "CD")
            if has_card:
                self.add_account(pid, "CREDIT_CARD")
            if has_loan:
                st = "PERSONAL_LOAN" if seg == "STUDENT" else self.choice(list(C.LOAN_MIX), p=list(C.LOAN_MIX.values()))
                od = None
                if self.chance(0.08):   # some loans originate inside the window
                    od = date(2026, int(r.integers(4, 9)), int(r.integers(1, 28)))
                self.add_account(pid, st, open_date=od)

        # secondary holders: joint (deposits), authorised users (cards), co-borrower/guarantor (loans)
        for a in list(self.accounts):
            if a["product_subtype"] == "CD" or not self.chance(C.P_SECONDARY_HOLDER):
                continue
            prim = next(iter(self.acct_parties[a["account_id"]]))
            role = {"DEPOSIT": "JOINT", "CARD": "AUTHORIZED_USER"}.get(
                a["product_type"], "GUARANTOR" if self.chance(0.25) else "CO_BORROWER")
            self.add_role(self.pick_secondary(prim), a["account_id"], role, a["open_date"])

    def secondaries(self, aid, roles=None):
        return [p for p, rl in self.acct_parties.get(aid, {}).items()
                if rl != "PRIMARY" and (roles is None or rl in roles)]

    # ======================================================================
    # NORMAL ACTIVITY
    # ======================================================================
    def gen_normal(self):
        # Students: some have a parent who pays their card / loan (legit third-party payments)
        self.parent_cp = {pid: self.cp("INDIVIDUAL") for pid in self.primary_ids
                          if self.party[pid]["segment"] == "STUDENT" and self.chance(0.4)}
        self.remitters = {pid for pid in self.primary_ids
                          if self.party[pid]["segment"] in ("MASS", "SELF_EMPLOYED") and self.chance(0.05)
                          and self.first(pid, "CHECKING")}
        for pid in self.primary_ids:
            self.normal_deposits(pid)
            for card in self.primary_accts[pid].get("CREDIT_CARD", []):
                self.normal_card(pid, card)
            for loan in self.loans_of(pid):
                self.normal_loan(pid, loan)

    def active(self, aid, ts):
        return ts >= self.dormant_until.get(aid, datetime.min)

    def normal_deposits(self, pid):
        p, r = self.party[pid], self.rng
        chk, sav = self.first(pid, "CHECKING"), self.first(pid, "SAVINGS")
        cd = self.first(pid, "CD")
        if not chk:
            return
        seg, net_month = p["segment"], p["annual_income"] * 0.75 / 12
        joint = self.secondaries(chk, {"JOINT"})
        who = lambda: joint[0] if joint and self.chance(0.3) else pid   # joint holder transacts sometimes
        has_mortgage = bool(self.primary_accts[pid].get("MORTGAGE"))
        has_card = bool(self.primary_accts[pid].get("CREDIT_CARD"))
        employer = self.cp("EMPLOYER")
        landlord = self.cp("LANDLORD")
        rent = 0.0
        if not has_mortgage and (seg != "RETIREE" or self.chance(0.5)) and (seg != "STUDENT" or self.chance(0.6)):
            rent = float(np.clip(p["annual_income"] / 12 * r.uniform(0.22, 0.32), 450, 6500))
        utils = [self.cp("UTILITY") for _ in range(int(r.integers(1, 4)))]
        bills = [self.cp("BILLER") for _ in range(int(r.integers(1, 4)))]
        remit_cp = None
        if pid in self.remitters:
            remit_cp = (self.choice(self.cp_pool["HR_ENTITY"][:40]) if self.chance(0.15)
                        else self.cp("REMIT"))   # a few legit remitters send to a high-risk country

        # salaried income: bi-weekly payroll across the whole window
        if seg in ("MASS", "MASS_AFFLUENT", "AFFLUENT"):
            d = C.WINDOW_START + timedelta(days=int(r.integers(0, 14)))
            while d <= C.WINDOW_END:
                ts = ts_on(d, r, 2, 6)
                if self.active(chk, ts):
                    self.add(chk, ts, "ACH_CREDIT", "CR", p["annual_income"] * 0.75 / 26 * r.uniform(0.97, 1.03),
                             "ACH", counterparty_id=employer, description="Payroll")
                d += timedelta(days=14)
        # tax refund (common, legit)
        if self.chance(0.25):
            d = datetime(2026, 4, 1) + timedelta(days=int(r.integers(0, 55)))
            self.add(chk, ts_on(d, r, 2, 6), "ACH_CREDIT", "CR", self.lognorm(2800, 0.6), "ACH",
                     counterparty_id=self.cp_pool["GOV_IRS"][0], description="IRS Tax Refund")

        for month in C.MONTHS:
            ms, nd = month_bounds(month)
            if not self.active(chk, ms + timedelta(days=nd)):
                continue
            # A(t) is True when the account is active at t. "A(t) and self.add(...)" relies on
            # short-circuit evaluation - Java: if (A(t)) { add(...); }
            A = lambda ts: self.active(chk, ts)
            # ---- income for non-salaried segments
            if seg == "STUDENT":
                if self.chance(0.6):
                    t = ts_on(rand_day(ms, nd, r, 1, 28), r)
                    A(t) and self.add(chk, t, "ACH_CREDIT", "CR", r.uniform(300, 1200), "ACH",
                                      counterparty_id=employer, description="Part-time payroll")
                if pid in self.parent_cp and self.chance(0.7):
                    t = ts_on(rand_day(ms, nd, r), r)
                    A(t) and self.add(chk, t, "P2P_IN", "CR", r.uniform(150, 800), "P2P",
                                      counterparty_id=self.parent_cp[pid], description="From parent")
            elif seg == "RETIREE":
                t = ts_on(ms + timedelta(days=2), r, 2, 6)
                A(t) and self.add(chk, t, "ACH_CREDIT", "CR", r.uniform(1200, 3200), "ACH",
                                  counterparty_id=self.cp_pool["GOV_SSA"][0], description="SSA Benefit")
                if p["annual_income"] > 40000:
                    t = ts_on(ms + timedelta(days=int(r.integers(0, 5))), r, 2, 6)
                    A(t) and self.add(chk, t, "ACH_CREDIT", "CR", p["annual_income"] * 0.4 / 12, "ACH",
                                      counterparty_id=self.cp("PENSION"), description="Pension")
            elif seg == "SELF_EMPLOYED":
                n = int(r.integers(3, 10))
                for amt in r.dirichlet(np.ones(n)) * net_month * r.uniform(0.7, 1.2):
                    t = ts_on(rand_day(ms, nd, r), r)
                    if A(t) and amt > 20:
                        typ = "ACH_CREDIT" if self.chance(0.6) else "CHECK_DEPOSIT"
                        self.add(chk, t, typ, "CR", amt, "ACH" if typ == "ACH_CREDIT" else "BRANCH",
                                 counterparty_id=self.cp("EMPLOYER"), description="Client payment")
                if p["is_cash_intensive"]:
                    n = int(r.integers(8, 20))
                    for amt in r.dirichlet(np.ones(n) * 3) * p["expected_monthly_cash"] * r.uniform(0.8, 1.2):
                        t = ts_on(rand_day(ms, nd, r), r, 9, 18)
                        A(t) and self.add(chk, t, "CASH_DEPOSIT", "CR", max(amt, 60), "BRANCH", is_cash=True,
                                          initiated_by_party_id=pid, description="Business cash takings")
                    if self.chance(0.12):   # legit weekend takings that land in the structuring band
                        t = ts_on(rand_day(ms, nd, r), r, 9, 18)
                        A(t) and self.add(chk, t, "CASH_DEPOSIT", "CR", r.uniform(8000, 9900), "BRANCH",
                                          is_cash=True, initiated_by_party_id=pid, description="Business cash takings")

            # ---- expenses
            if rent:
                t = ts_on(ms + timedelta(days=int(r.integers(0, 3))), r)
                A(t) and self.add(chk, t, "ACH_DEBIT", "DR", rent, "ACH", counterparty_id=landlord, description="Rent")
            for u in utils:
                t = ts_on(rand_day(ms, nd, r), r)
                A(t) and self.add(chk, t, "ACH_DEBIT", "DR", self.lognorm(120, 0.4), "ACH", counterparty_id=u,
                                  description="Utility bill")
            for b in bills:
                t = ts_on(rand_day(ms, nd, r), r)
                A(t) and self.add(chk, t, "ACH_DEBIT", "DR", self.lognorm(70, 0.6), "ACH", counterparty_id=b,
                                  description="Bill payment")
            n_pos = int(r.integers(4, 14) if has_card else r.integers(10, 30))
            if seg == "STUDENT":
                n_pos = int(n_pos * 0.7)
            mccs, w = list(C.NORMAL_MCC_WEIGHTS), np.array(list(C.NORMAL_MCC_WEIGHTS.values()))
            for _ in range(n_pos):
                m = self.choice(mccs, p=w / w.sum())
                t = ts_on(rand_day(ms, nd, r), r, 7, 23)
                if A(t):
                    self.add(chk, t, "POS_PURCHASE", "DR", self.lognorm(C.MCC[m][1], C.MCC[m][2]), "POS",
                             counterparty_id=self.choice(self.merchants[m]), mcc=m, initiated_by_party_id=who(),
                             description=f"Debit card - {C.MCC[m][0]}")
            for _ in range(int(r.integers(0, 4) + (seg in ("MASS", "RETIREE")))):
                t = ts_on(rand_day(ms, nd, r), r, 6, 23)
                st = p["state"] if self.chance(0.93) else self.choice(C.US_STATES)
                A(t) and self.add(chk, t, "ATM_WITHDRAWAL", "DR", 20 * int(r.integers(2, 21)), "ATM",
                                  is_cash=True, location_state=st, initiated_by_party_id=who(),
                                  description="ATM withdrawal")
            for _ in range(int(r.integers(0, 5))):
                t = ts_on(rand_day(ms, nd, r), r)
                A(t) and self.add(chk, t, "P2P_OUT", "DR", self.lognorm(90, 0.8), "P2P",
                                  counterparty_id=self.cp("INDIVIDUAL"), initiated_by_party_id=who(), description="Zelle")
            for _ in range(int(r.integers(0, 3))):
                t = ts_on(rand_day(ms, nd, r), r)
                A(t) and self.add(chk, t, "P2P_IN", "CR", self.lognorm(80, 0.8), "P2P",
                                  counterparty_id=self.cp("INDIVIDUAL"), description="Zelle")
            if self.chance(0.15) and not p["is_cash_intensive"]:
                t = ts_on(rand_day(ms, nd, r), r, 9, 17)
                A(t) and self.add(chk, t, "CASH_DEPOSIT", "CR", self.lognorm(300, 0.7), "BRANCH", is_cash=True,
                                  initiated_by_party_id=who(), description="Cash deposit")
            if self.chance(0.10):
                t = ts_on(rand_day(ms, nd, r), r)
                A(t) and self.add(chk, t, "CHECK_PAID", "DR", self.lognorm(600, 0.8), "CHECK",
                                  counterparty_id=self.cp("INDIVIDUAL"), description="Check paid")
            if seg == "AFFLUENT" and self.chance(0.3):
                t = ts_on(rand_day(ms, nd, r), r, 9, 16)
                A(t) and self.add(chk, t, "WIRE_OUT", "DR", self.lognorm(9000, 0.6), "WIRE",
                                  counterparty_id=self.cp("BROKERAGE"), description="Wire to brokerage")
            if remit_cp:
                t = ts_on(rand_day(ms, nd, r), r, 9, 17)
                A(t) and self.add(chk, t, "WIRE_OUT", "DR", r.uniform(200, 1800), "WIRE",
                                  counterparty_id=remit_cp, cp_country=self.cp_country(remit_cp),
                                  description="Family remittance")
            if sav:
                t = ts_on(ms + timedelta(days=int(r.integers(1, 6))), r)
                if A(t) and self.active(sav, t):
                    self.xfer(chk, sav, t, max(25, net_month * r.uniform(0.03, 0.10)), "TRANSFER_OUT",
                              "TRANSFER_IN", pid, "To savings")
                    self.add(sav, ms + timedelta(days=nd - 1, hours=23), "INTEREST", "CR", r.uniform(0.5, 40),
                             "SYSTEM", description="Interest")
            if cd:
                self.add(cd, ms + timedelta(days=nd - 1, hours=23), "INTEREST", "CR", r.uniform(10, 120),
                         "SYSTEM", description="CD interest")

    def cp_country(self, cp_id):
        # Counterparty IDs are sequential "CP000123" -> index 122 in self.cps
        return self.cps[int(cp_id[2:]) - 1]["country"]

    def normal_card(self, pid, card):
        p, r, a = self.party[pid], self.rng, self.acct[card]
        lim, seg = a["credit_limit"], p["segment"]
        chk = self.first(pid, "CHECKING")
        au = self.secondaries(card, {"AUTHORIZED_USER"})
        spend = {"MASS": (500, 1500), "MASS_AFFLUENT": (1500, 4000), "AFFLUENT": (4000, 12000),
                 "STUDENT": (150, 600), "RETIREE": (400, 1500), "SELF_EMPLOYED": (1000, 4000)}[seg]
        monthly_spend = float(r.uniform(*spend))
        pays_full = self.chance(0.6)
        if pid in self.parent_cp and self.chance(0.6):
            payer = ("PARENT", self.parent_cp[pid])
        elif chk and self.chance(0.85):
            payer = ("INTERNAL", chk)
        else:
            payer = ("EXT", self.self_external(pid))
        pay_day = int(r.integers(15, 26))
        balance = float(lim * r.uniform(0.0, 0.35))
        mccs, w = list(C.NORMAL_MCC_WEIGHTS), np.array(list(C.NORMAL_MCC_WEIGHTS.values()))
        stop = self.card_stop_on.get(card, datetime.max)
        cash_adv_month = self.choice(C.MONTHS) if self.chance(0.03) else None

        for month in C.MONTHS:
            ms, nd = month_bounds(month)
            # ---- payment of last statement
            pay_ts = ts_on(ms + timedelta(days=pay_day - 1), r)
            if pay_ts < stop and balance > 5:
                amt = balance if pays_full else max(25.0, balance * r.uniform(0.05, 0.4))
                amt = round(amt, 2)
                if payer[0] == "INTERNAL":
                    self.xfer(payer[1], card, pay_ts, amt, "CARD_PAYMENT_OUT", "PAYMENT", pid, "Card payment")
                else:
                    self.add(card, pay_ts, "PAYMENT", "CR", amt, "ACH", counterparty_id=payer[1],
                             description="Card payment (external)")
                balance -= amt
                if not pays_full and balance > 0:
                    intr = balance * 0.22 / 12
                    self.add(card, ms + timedelta(days=nd - 1, hours=22), "INTEREST_CHARGE", "DR", intr, "SYSTEM",
                             description="Interest charge")
                    balance += intr
            # ---- purchases
            target = monthly_spend * r.uniform(0.7, 1.3)
            spent = 0.0
            while spent < target:
                m = self.choice(mccs, p=w / w.sum())
                amt = self.lognorm(C.MCC[m][1], C.MCC[m][2])
                t = ts_on(rand_day(ms, nd, r), r, 7, 23)
                if t >= stop or balance + amt > lim * 0.95:
                    break
                by = au[0] if au and self.chance(0.35) else pid
                self.add(card, t, "PURCHASE", "DR", amt, "POS" if self.chance(0.7) else "ONLINE",
                         counterparty_id=self.choice(self.merchants[m]), mcc=m, initiated_by_party_id=by,
                         description=C.MCC[m][0])
                spent += amt
                balance += amt
            if self.chance(0.2):
                t = ts_on(rand_day(ms, nd, r), r)
                if t < stop:
                    amt = min(self.lognorm(60, 0.7), balance)
                    if amt > 1:
                        self.add(card, t, "MERCHANT_REFUND", "CR", amt, "POS", counterparty_id=self.choice(
                            self.merchants["5999"]), mcc="5999", description="Merchant refund")
                        balance -= amt
            if month == cash_adv_month:
                t = ts_on(rand_day(ms, nd, r), r)
                amt = 20 * int(r.integers(10, 41))
                if t < stop and balance + amt < lim:
                    self.add(card, t, "CASH_ADVANCE", "DR", amt, "ATM", is_cash=True, initiated_by_party_id=pid,
                             description="Cash advance")
                    balance += amt

    def normal_loan(self, pid, loan):
        p, r, a = self.party[pid], self.rng, self.acct[loan]
        chk = self.first(pid, "CHECKING")
        open_dt = datetime.combine(a["open_date"], datetime.min.time())
        closed = self.loan_closed_on.get(loan, datetime.max)
        co = self.secondaries(loan, {"CO_BORROWER"})
        if open_dt >= C.WINDOW_START and not a.get("_skip_disb"):   # originated inside the window
            t = ts_on(open_dt, r, 9, 17)
            if a["product_subtype"] == "PERSONAL_LOAN" and chk:
                self.xfer(loan, chk, t, a["loan_amount"], "DISBURSEMENT", "LOAN_DISBURSEMENT_IN", pid,
                          "Personal loan proceeds")
            else:
                pool = {"AUTO_LOAN": "AUTO_DEALER", "MORTGAGE": "TITLE_CO"}.get(a["product_subtype"])
                cp = self.cp(pool) if pool else self.self_external(pid)
                self.add(loan, t, "DISBURSEMENT", "DR", a["loan_amount"], "WIRE" if pool else "ACH",
                         counterparty_id=cp, description="Loan disbursement")
        pay_day = int(r.integers(1, 16))
        extra_month = self.choice(C.MONTHS) if self.chance(0.05) else None
        skip = a.get("_skip_months", set())
        for month in C.MONTHS:
            ms, nd = month_bounds(month)
            t = ts_on(ms + timedelta(days=pay_day - 1), r)
            if t <= open_dt + timedelta(days=25) or t >= closed or month in skip:
                continue
            amt = a["monthly_payment"]
            if co and self.chance(0.4):
                self.add(loan, t, "PAYMENT", "CR", amt, "ACH", counterparty_id=self.self_external(co[0]),
                         description="Loan payment (co-borrower)")
            elif pid in self.parent_cp and self.chance(0.5):
                self.add(loan, t, "PAYMENT", "CR", amt, "ACH", counterparty_id=self.parent_cp[pid],
                         description="Loan payment (parent)")
            elif chk and self.chance(0.92):
                self.xfer(chk, loan, t, amt, "LOAN_PAYMENT_OUT", "PAYMENT", pid, "Loan payment")
            else:
                self.add(loan, t, "PAYMENT", "CR", amt, "ACH", counterparty_id=self.self_external(pid),
                         description="Loan payment (external)")
            if month == extra_month:
                t2 = t + timedelta(days=int(r.integers(1, 10)))
                if t2 < closed:
                    extra = float(r.uniform(200, 3000))
                    if chk:
                        self.xfer(chk, loan, t2, extra, "LOAN_PAYMENT_OUT", "EXTRA_PAYMENT", pid, "Extra principal")
                    else:
                        self.add(loan, t2, "EXTRA_PAYMENT", "CR", extra, "ACH",
                                 counterparty_id=self.self_external(pid), description="Extra principal")


    # ======================================================================
    # SAVE
    # ======================================================================
    def save(self, out_dir="data"):
        import os
        os.makedirs(out_dir, exist_ok=True)
        tx = self.txn.to_frame()
        # drop anything a scheme pushed past the window edge (and its labels)
        tx = tx[(tx.txn_ts >= C.WINDOW_START) & (tx.txn_ts <= C.WINDOW_END)].reset_index(drop=True)
        lab = pd.DataFrame(self.label_rows, columns=["txn_id", "scheme_id", "typology"])
        lab = lab[lab.txn_id.isin(tx.txn_id)].drop_duplicates("txn_id")

        parties = pd.DataFrame(self.parties)
        accounts = pd.DataFrame([{k: v for k, v in a.items() if not k.startswith("_")} for a in self.accounts])
        roles = pd.DataFrame(self.roles)
        cps = pd.DataFrame(self.cps)

        # ---- labels at account and party level
        lab_acct = (lab.merge(tx[["txn_id", "account_id"]], on="txn_id")
                       .groupby("account_id")
                       .agg(typologies=("typology", lambda s: ",".join(sorted(set(s)))),
                            scheme_ids=("scheme_id", lambda s: ",".join(sorted(set(s)))),
                            n_suspicious_txns=("txn_id", "size"))
                       .reset_index())
        labels_accounts = accounts[["account_id"]].merge(lab_acct, on="account_id", how="left")
        labels_accounts["is_suspicious"] = labels_accounts.typologies.notna()

        spr = pd.DataFrame(self.scheme_party_roles, columns=["scheme_id", "party_id", "role_in_scheme"])
        spr = spr.merge(pd.DataFrame([{"scheme_id": s["scheme_id"], "typology": s["typology"]}
                                      for s in self.schemes]), on="scheme_id")
        bad = spr[spr.role_in_scheme != "INNOCENT_PRIMARY"]
        agg = bad.groupby("party_id").agg(typologies=("typology", lambda s: ",".join(sorted(set(s)))),
                                          role_in_scheme=("role_in_scheme", lambda s: ",".join(sorted(set(s)))),
                                          scheme_ids=("scheme_id", lambda s: ",".join(sorted(set(s))))).reset_index()
        labels_parties = parties[["party_id"]].merge(agg, on="party_id", how="left")
        labels_parties["is_suspicious"] = labels_parties.typologies.notna()
        labels_parties["innocent_primary_on_abused_account"] = labels_parties.party_id.isin(
            spr[spr.role_in_scheme == "INNOCENT_PRIMARY"].party_id)

        schemes = pd.DataFrame([{
            "scheme_id": s["scheme_id"], "typology": s["typology"], "intensity": s["intensity"],
            "primary_party_id": s["primary_party_id"],
            "party_ids": ",".join(spr[spr.scheme_id == s["scheme_id"]].party_id),
            "account_ids": ",".join(a for a in s["accounts"] if a), "months": ",".join(s["months"]),
            "n_txns": int((lab.scheme_id == s["scheme_id"]).sum())} for s in self.schemes])
        spikes = pd.DataFrame(self.spikes)

        tables = dict(parties=parties, accounts=accounts, party_account_role=roles, counterparties=cps,
                      transactions=tx, labels_transactions=lab, labels_accounts=labels_accounts,
                      labels_parties=labels_parties, schemes=schemes, legit_spikes=spikes)
        for name, df in tables.items():
            df.to_parquet(f"{out_dir}/{name}.parquet", index=False)
        # product views (same rows, filtered) for people who think in product silos
        for name, prod in (("deposit_txns", "DEPOSIT"), ("card_txns", "CARD"), ("loan_ledger", "LOAN")):
            tx[tx.product_type == prod].to_parquet(f"{out_dir}/{name}.parquet", index=False)
        return tables


def build(out_dir="data"):
    from typologies import SchemePlanner, SchemeInjector, plan_spikes, inject_spikes, inject_lookalikes
    bank = SyntheticBank()
    bank.build_counterparties()
    bank.build_parties()
    bank.build_accounts()
    planner = SchemePlanner(bank)
    planner.plan()                    # phase 1: decide who launders, set behavioural constraints
    plan_spikes(bank, planner.used)
    bank.gen_normal()                 # phase 2: normal retail behaviour for everyone
    inject_spikes(bank)               # phase 3: legit-but-unusual one-off events (FP fodder)
    inject_lookalikes(bank, planner.used)  # phase 3b: ongoing legit behaviour that trips rules
    SchemeInjector(bank).inject()     # phase 4: labelled laundering schemes
    return bank.save(out_dir)


if __name__ == "__main__":       # Java: public static void main(String[] args)
    import time
    t0 = time.time()
    T = build()
    for k, v in T.items():
        print(f"{k:22s} {len(v):>10,d}")
    tx = T["transactions"]
    print("\nby product:\n", tx.product_type.value_counts().to_string())
    lp = T["labels_parties"]
    print(f"\nsuspicious parties: {lp.is_suspicious.sum()} / {len(lp)} "
          f"({lp.is_suspicious.mean():.2%}); suspicious txns: {len(T['labels_transactions']):,}")
    print(f"done in {time.time() - t0:.0f}s")
