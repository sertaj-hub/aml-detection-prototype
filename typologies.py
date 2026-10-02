"""
typologies.py - Plans and injects (1) labelled money-laundering schemes and (2) legitimate
"looks suspicious but isn't" spikes into a SyntheticBank.

Two phases, because some schemes change what normal activity looks like
(e.g. a dormant account must have NO normal activity before it wakes up):
  plan_*   : runs BEFORE normal activity - picks parties, creates accounts, sets constraints
  inject_* : runs AFTER normal activity - writes the scheme / spike transactions and labels

Every scheme transaction is labelled (txn_id, scheme_id, typology) -> ground truth.
"""
from datetime import datetime, timedelta, date
import numpy as np

import config as C
from common import month_bounds, ts_on, amortized_payment

# typology: (number of schemes, preferred segments)
SCHEME_PLAN = {
    "STRUCT":            (8, ["MASS", "MASS_AFFLUENT", "SELF_EMPLOYED"]),
    "RAPID_MOVE":        (6, ["SELF_EMPLOYED", "MASS_AFFLUENT", "AFFLUENT"]),
    "FUNNEL":            (5, ["MASS", "STUDENT"]),
    "MULE_RING":         (3, ["STUDENT", "MASS"]),          # each ring = 5-6 mules + 1 collector
    "HR_WIRE":           (5, ["MASS", "MASS_AFFLUENT", "SELF_EMPLOYED"]),
    "DORMANT":           (4, ["MASS", "MASS_AFFLUENT", "RETIREE"]),
    "CASH_PROFILE":      (5, ["STUDENT", "RETIREE", "MASS"]),
    "CC_OVERPAY_REFUND": (4, ["MASS", "MASS_AFFLUENT"]),
    "CC_CASH_ADV":       (3, ["MASS", "SELF_EMPLOYED"]),
    "CC_3P_PAY":         (3, ["MASS", "STUDENT"]),
    "CC_HRMCC":          (3, ["MASS", "MASS_AFFLUENT"]),
    "CC_BUSTOUT":        (2, ["MASS"]),
    "LN_EARLY_PAYOFF":   (3, ["MASS", "MASS_AFFLUENT", "SELF_EMPLOYED"]),
    "LN_RAPID_OUT":      (3, ["MASS", "MASS_AFFLUENT"]),
    "LN_3P_REPAY":       (2, ["MASS", "STUDENT"]),
    "XP_CASH_TO_CREDIT": (5, ["MASS", "MASS_AFFLUENT", "SELF_EMPLOYED"]),
    "JOINT_BAD_ACTOR":   (3, ["MASS", "RETIREE"]),
    "THIN_SPREAD":       (4, ["MASS", "STUDENT", "RETIREE"]),
}
P_SUBTLE = 0.35   # share of schemes deliberately run "below the radar" of simple rules


def _dt(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time())


class SchemePlanner:
    def __init__(self, bank):
        self.b, self.r = bank, bank.rng
        self.used = set()

    # ---------------------------------------------------------------- helpers
    def pick_party(self, segments, need=(), not_cash_intensive=True):
        b = self.b
        cands = [pid for pid in b.primary_ids
                 if pid not in self.used and b.party[pid]["segment"] in segments
                 and not (not_cash_intensive and b.party[pid]["is_cash_intensive"])]
        pid = b.choice(cands)
        self.used.add(pid)
        for sub in need:      # make sure required products exist
            if not b.first(pid, sub):
                b.add_account(pid, sub)
        return pid

    def window(self, n_months):
        """Random contiguous run of n months inside the 6-month window."""
        start = int(self.r.integers(0, len(C.MONTHS) - n_months + 1))
        return C.MONTHS[start:start + n_months]

    def new_scheme(self, typ, party_roles, accounts, months, **params):
        b = self.b
        sid = b.ids["scheme"].next()
        s = dict(scheme_id=sid, typology=typ, primary_party_id=party_roles[0][0], accounts=accounts,
                 months=months, intensity="SUBTLE" if b.chance(P_SUBTLE) else "STRONG", params=params)
        # every typology gets at least one STRONG example; mule rings are always strong
        if typ == "MULE_RING" or not any(x["typology"] == typ for x in b.schemes):
            s["intensity"] = "STRONG"
        b.schemes.append(s)
        for pid, role in party_roles:
            b.scheme_party_roles.append((sid, pid, role))
        # shift the party's KYC risk a little (real bad actors aren't always rated high)
        p = b.party[party_roles[0][0]]
        if b.chance(0.3):
            p["kyc_risk"] = "MEDIUM" if p["kyc_risk"] == "LOW" else "HIGH"
        return s

    # ------------------------------------------------------------------ plan
    def plan(self):
        b, r = self.b, self.r
        for typ, (n, segs) in SCHEME_PLAN.items():
            for _ in range(n):
                getattr(self, f"plan_{typ.lower()}")(segs)   # Java: reflection-style dispatch

    def plan_struct(self, segs):
        pid = self.pick_party(segs, need=["CHECKING"])
        self.new_scheme("STRUCT", [(pid, "ACTOR")], [self.b.first(pid, "CHECKING"), self.b.first(pid, "SAVINGS")],
                        self.window(int(self.r.integers(2, 4))))

    def plan_rapid_move(self, segs):
        pid = self.pick_party(segs, need=["CHECKING"])
        self.new_scheme("RAPID_MOVE", [(pid, "ACTOR")], [self.b.first(pid, "CHECKING")],
                        self.window(int(self.r.integers(2, 4))))

    def plan_funnel(self, segs):
        pid = self.pick_party(segs, need=["CHECKING"])
        self.new_scheme("FUNNEL", [(pid, "ACTOR")], [self.b.first(pid, "CHECKING")],
                        self.window(int(self.r.integers(2, 4))))

    def plan_mule_ring(self, segs):
        coll = self.pick_party(["MASS", "SELF_EMPLOYED"], need=["CHECKING"])
        mules = [self.pick_party(segs, need=["CHECKING"]) for _ in range(int(self.r.integers(5, 7)))]
        roles = [(coll, "COLLECTOR")] + [(m, "MULE") for m in mules]
        self.new_scheme("MULE_RING", roles, [self.b.first(p, "CHECKING") for p, _ in roles],
                        self.window(int(self.r.integers(2, 5))), collector=coll, mules=mules)

    def plan_hr_wire(self, segs):
        pid = self.pick_party(segs, need=["CHECKING"])
        self.new_scheme("HR_WIRE", [(pid, "ACTOR")], [self.b.first(pid, "CHECKING")],
                        self.window(int(self.r.integers(2, 5))))

    def plan_dormant(self, segs):
        b, r = self.b, self.r
        cands = [p for p in b.primary_ids if p not in self.used and b.party[p]["segment"] in segs
                 and b.party[p]["onboarding_date"] < date(2021, 1, 1) and b.first(p, "CHECKING")]
        pid = b.choice(cands)
        self.used.add(pid)
        old = date(int(r.integers(2014, 2021)), int(r.integers(1, 13)), 1)
        dorm = b.add_account(pid, "SAVINGS", open_date=old)
        b.acct[dorm]["last_activity_pre_window"] = date(int(r.integers(2023, 2025)), int(r.integers(1, 13)), 15)
        wake = datetime(2026, int(r.integers(7, 9)), int(r.integers(1, 25)))
        b.dormant_until[dorm] = wake
        self.new_scheme("DORMANT", [(pid, "ACTOR")], [dorm, b.first(pid, "CHECKING")],
                        [f"2026-{wake.month:02d}", f"2026-{wake.month + 1:02d}"], dormant=dorm, wake=wake)

    def plan_cash_profile(self, segs):
        pid = self.pick_party(segs, need=["CHECKING"])
        self.new_scheme("CASH_PROFILE", [(pid, "ACTOR")], [self.b.first(pid, "CHECKING")],
                        self.window(int(self.r.integers(2, 4))))

    def _card_scheme(self, typ, segs, months, limit=None, need=("CREDIT_CARD", "CHECKING")):
        pid = self.pick_party(segs, need=list(need))
        card = self.b.first(pid, "CREDIT_CARD")
        if limit:
            self.b.acct[card]["credit_limit"] = float(max(self.b.acct[card]["credit_limit"], limit))
        return self.new_scheme(typ, [(pid, "ACTOR")], [card, self.b.first(pid, "CHECKING")], months)

    def plan_cc_overpay_refund(self, segs):
        self._card_scheme("CC_OVERPAY_REFUND", segs, self.window(int(self.r.integers(2, 4))))

    def plan_cc_cash_adv(self, segs):
        self._card_scheme("CC_CASH_ADV", segs, self.window(int(self.r.integers(2, 4))), limit=12000)

    def plan_cc_3p_pay(self, segs):
        self._card_scheme("CC_3P_PAY", segs, self.window(int(self.r.integers(2, 4))), limit=15000)

    def plan_cc_hrmcc(self, segs):
        self._card_scheme("CC_HRMCC", segs, self.window(int(self.r.integers(2, 5))), limit=25000)

    def plan_cc_bustout(self, segs):
        s = self._card_scheme("CC_BUSTOUT", segs, self.window(1))
        card = s["accounts"][0]
        self.b.acct[card]["credit_limit"] = float(round(self.r.uniform(6000, 15000), -2))
        ms, nd = month_bounds(s["months"][0])
        start = ms + timedelta(days=int(self.r.integers(0, 8)))
        self.b.card_stop_on[card] = start
        s["params"]["bust_start"] = start

    def plan_ln_early_payoff(self, segs):
        b, r = self.b, self.r
        pid = self.pick_party(segs, need=["CHECKING"])
        od = date(2025, int(r.integers(7, 13)), int(r.integers(1, 28)))
        loan = b.add_account(pid, self.b.choice(["PERSONAL_LOAN", "AUTO_LOAN"]), open_date=od)
        b.acct[loan]["loan_amount"] = float(round(r.uniform(15000, 40000), -2))
        a = b.acct[loan]
        a["monthly_payment"] = round(amortized_payment(a["loan_amount"], a["interest_rate"], a["loan_term_months"]), 2)
        months = self.window(2)
        ms, nd = month_bounds(months[-1])
        payoff = ms + timedelta(days=int(r.integers(5, 25)), hours=12)
        b.loan_closed_on[loan] = payoff
        self.new_scheme("LN_EARLY_PAYOFF", [(pid, "ACTOR")], [loan, b.first(pid, "CHECKING")], months,
                        loan=loan, payoff=payoff, funding=b.choice(["CASH", "OFFSHORE_WIRE", "THIRD_PARTY"]))

    def plan_ln_rapid_out(self, segs):
        b, r = self.b, self.r
        pid = self.pick_party(segs, need=["CHECKING"])
        od = date(2026, int(r.integers(4, 9)), int(r.integers(1, 25)))
        loan = b.add_account(pid, "PERSONAL_LOAN", open_date=od, _skip_disb=True)
        b.acct[loan]["loan_amount"] = float(round(r.uniform(15000, 50000), -2))
        a = b.acct[loan]
        a["monthly_payment"] = round(amortized_payment(a["loan_amount"], a["interest_rate"], a["loan_term_months"]), 2)
        self.new_scheme("LN_RAPID_OUT", [(pid, "ACTOR")], [loan, b.first(pid, "CHECKING")],
                        [f"2026-{od.month:02d}"], loan=loan)

    def plan_ln_3p_repay(self, segs):
        b = self.b
        pid = self.pick_party(segs, need=["CHECKING"])
        loans = b.loans_of(pid)
        loan = loans[0] if loans else b.add_account(pid, "PERSONAL_LOAN", open_date=date(2025, 6, 10))
        months = self.window(int(self.r.integers(2, 5)))
        b.acct[loan]["_skip_months"] = set(months)
        self.new_scheme("LN_3P_REPAY", [(pid, "ACTOR")], [loan], months, loan=loan)

    def plan_xp_cash_to_credit(self, segs):
        s = self._card_scheme("XP_CASH_TO_CREDIT", segs, self.window(int(self.r.integers(2, 4))), limit=30000)
        loans = self.b.loans_of(s["primary_party_id"])
        s["params"]["loan"] = loans[0] if loans else None
        if loans:
            s["accounts"].append(loans[0])

    def plan_joint_bad_actor(self, segs):
        b, r = self.b, self.r
        innocent = self.pick_party(segs, need=["CHECKING"])
        chk = b.first(innocent, "CHECKING")
        bad = b.choice([p for p in b.secondary_ids if p not in self.used])
        self.used.add(bad)
        if bad not in b.acct_parties.get(chk, {}):
            b.add_role(bad, chk, "JOINT", date(2025, int(r.integers(1, 12)), 1))
        self.new_scheme("JOINT_BAD_ACTOR", [(bad, "ACTOR"), (innocent, "INNOCENT_PRIMARY")], [chk],
                        self.window(int(self.r.integers(2, 4))), bad_actor=bad, innocent=innocent)

    def plan_thin_spread(self, segs):
        pid = self.pick_party(segs, need=["CHECKING", "SAVINGS", "CREDIT_CARD"])
        b = self.b
        b.acct[b.first(pid, "CREDIT_CARD")]["credit_limit"] = float(
            max(b.acct[b.first(pid, "CREDIT_CARD")]["credit_limit"], 15000))
        accts = [b.first(pid, "CHECKING"), b.first(pid, "SAVINGS"), b.first(pid, "CREDIT_CARD")] + b.loans_of(pid)[:1]
        self.new_scheme("THIN_SPREAD", [(pid, "ACTOR")], accts, self.window(3),
                        loan=(b.loans_of(pid) or [None])[0])


# =============================================================================
# INJECTION
# =============================================================================
class SchemeInjector:
    def __init__(self, bank):
        self.b, self.r = bank, bank.rng

    def ts_between(self, start, end, h_lo=8, h_hi=19):
        span = max((end - start).days, 0)
        d = start + timedelta(days=int(self.r.integers(0, span + 1)))
        return ts_on(d, self.r, h_lo, h_hi)

    def month_span(self, months):
        s, _ = month_bounds(months[0])
        e, nd = month_bounds(months[-1])
        return s, e + timedelta(days=nd - 1)

    def inject(self):
        for s in self.b.schemes:
            self.s = s
            self.L = lambda tid: self.b.label(tid, s["scheme_id"], s["typology"])   # label helper
            getattr(self, f"inj_{s['typology'].lower()}")(s, s["intensity"] == "SUBTLE")

    def L2(self, ids):   # label both legs of a transfer
        for t in ids:
            self.L(t)

    # -------------------------------------------------------------- deposits
    def inj_struct(self, s, subtle):
        b, r = self.b, self.r
        pid, (chk, sav) = s["primary_party_id"], s["accounts"]
        start, end = self.month_span(s["months"])
        week = start + timedelta(days=int(r.integers(0, 7)))
        while week + timedelta(days=7) <= end:
            if b.chance(0.75):
                n = 2 if subtle else int(r.integers(3, 6))
                total = 0
                for i in range(n):
                    tgt = sav if (subtle and sav and i % 2) else chk
                    t = ts_on(week + timedelta(days=int(r.integers(0, 6))), r, 9, 17)
                    amt = r.uniform(8000, 9900)
                    st = b.party[pid]["state"] if b.chance(0.7) else b.choice(C.US_STATES)
                    self.L(b.add(tgt, t, "CASH_DEPOSIT", "CR", amt, b.choice(["BRANCH", "BRANCH", "ATM"]),
                                 is_cash=True, location_state=st, initiated_by_party_id=pid, description="Cash deposit"))
                    if tgt == sav:
                        self.L2(b.xfer(sav, chk, t + timedelta(hours=3), amt, "TRANSFER_OUT", "TRANSFER_IN", pid))
                    total += amt
                t = ts_on(week + timedelta(days=int(r.integers(6, 10))), r, 9, 16)
                cp = b.cp(b.choice(["OFFSHORE", "INDIVIDUAL", "INDIVIDUAL"]))
                self.L(b.add(chk, t, "WIRE_OUT", "DR", total * r.uniform(0.8, 0.95), "WIRE", counterparty_id=cp,
                             cp_country=b.cp_country(cp), initiated_by_party_id=pid, description="Wire out"))
            week += timedelta(days=int(r.integers(7, 12)))

    def inj_rapid_move(self, s, subtle):
        b, r = self.b, self.r
        pid, chk = s["primary_party_id"], s["accounts"][0]
        start, end = self.month_span(s["months"])
        for _ in range(int(r.integers(2, 6))):
            t = self.ts_between(start, end - timedelta(days=4), 9, 15)
            amt = r.uniform(12000, 19500) if subtle else r.uniform(25000, 90000)
            src = b.cp(b.choice(["OFFSHORE", "OFFSHORE", "EMPLOYER"]))
            self.L(b.add(chk, t, "WIRE_IN", "CR", amt, "WIRE", counterparty_id=src, cp_country=b.cp_country(src),
                         description="Incoming wire"))
            out = amt * r.uniform(0.88, 0.98)
            for part in r.dirichlet(np.ones(int(r.integers(1, 4)))) * out:
                t2 = t + timedelta(days=int(r.integers(0, 3)), hours=int(r.integers(1, 6)))
                cp = b.cp(b.choice(["OFFSHORE", "INDIVIDUAL", "CRYPTO"]))
                self.L(b.add(chk, t2, "WIRE_OUT", "DR", part, "WIRE", counterparty_id=cp,
                             cp_country=b.cp_country(cp), initiated_by_party_id=pid, description="Outgoing wire"))

    def inj_funnel(self, s, subtle):
        b, r = self.b, self.r
        pid, chk = s["primary_party_id"], s["accounts"][0]
        for m in s["months"]:
            ms, nd = month_bounds(m)
            states = list(r.choice(C.US_STATES, size=int(r.integers(2, 4) if subtle else r.integers(4, 9)),
                                   replace=False))
            total = 0
            for _ in range(int(r.integers(5, 8) if subtle else r.integers(8, 19))):
                t = ts_on(ms + timedelta(days=int(r.integers(0, nd - 3))), r, 9, 17)
                amt = r.uniform(900, 4800)
                self.L(b.add(chk, t, "CASH_DEPOSIT", "CR", amt, "BRANCH", is_cash=True,
                             location_state=str(b.choice(states)), counterparty_id=b.cp("INDIVIDUAL"),
                             description="Cash deposit (third-party depositor)"))
                total += amt
            out = total * r.uniform(0.8, 0.95)
            wired = out * r.uniform(0.5, 0.8)
            cp = b.cp(b.choice(["HR_ENTITY", "OFFSHORE"]))
            self.L(b.add(chk, ms + timedelta(days=nd - 2, hours=11), "WIRE_OUT", "DR", wired, "WIRE",
                         counterparty_id=cp, cp_country=b.cp_country(cp), initiated_by_party_id=pid,
                         description="Wire out"))
            far = b.choice([s_ for s_ in C.US_STATES if s_ not in states])
            left = out - wired
            while left > 200:
                amt = min(left, 20 * int(r.integers(20, 41)))
                t = ts_on(ms + timedelta(days=int(r.integers(3, nd))), r, 0, 23)
                self.L(b.add(chk, t, "ATM_WITHDRAWAL", "DR", amt, "ATM", is_cash=True, location_state=far,
                             initiated_by_party_id=pid, description="ATM withdrawal"))
                left -= amt

    def inj_mule_ring(self, s, subtle):
        b, r = self.b, self.r
        coll = s["params"]["collector"]
        coll_chk = b.first(coll, "CHECKING")
        received = 0
        for mule in s["params"]["mules"]:
            mchk = b.first(mule, "CHECKING")
            weak = b.chance(0.3)
            for m in s["months"]:
                ms, nd = month_bounds(m)
                for _ in range(int(r.integers(3, 6) if weak else r.integers(5, 13))):
                    t = ts_on(ms + timedelta(days=int(r.integers(0, nd - 2))), r, 7, 22)
                    amt = r.uniform(300, 2500)
                    typ = b.choice(["P2P_IN", "P2P_IN", "ACH_CREDIT"])
                    self.L(b.add(mchk, t, typ, "CR", amt, "P2P" if typ == "P2P_IN" else "ACH",
                                 counterparty_id=b.cp("INDIVIDUAL"), description="Incoming transfer"))
                    fwd = amt * r.uniform(0.85, 0.95)
                    t2 = t + timedelta(hours=int(r.integers(2, 40)))
                    if b.chance(0.75):
                        self.L(b.add(mchk, t2, "P2P_OUT", "DR", fwd, "P2P", counter_account_id=coll_chk,
                                     initiated_by_party_id=mule, description="Zelle"))
                        self.L(b.add(coll_chk, t2, "P2P_IN", "CR", fwd, "P2P", counter_account_id=mchk,
                                     description="Zelle"))
                        received += fwd
                    else:
                        self.L(b.add(mchk, t2, "ATM_WITHDRAWAL", "DR", 20 * round(fwd / 20), "ATM", is_cash=True,
                                     initiated_by_party_id=mule, description="ATM withdrawal"))
        # collector cashes out weekly
        start, end = self.month_span(s["months"])
        weeks = max(1, (end - start).days // 7)
        for i in range(weeks):
            t = ts_on(start + timedelta(days=7 * i + int(r.integers(3, 7))), r, 9, 16)
            cp = b.cp(b.choice(["HR_ENTITY", "CRYPTO", "CRYPTO"]))
            self.L(b.add(coll_chk, t, "WIRE_OUT", "DR", received / weeks * r.uniform(0.85, 0.95), "WIRE",
                         counterparty_id=cp, cp_country=b.cp_country(cp), initiated_by_party_id=coll,
                         description="Wire out"))

    def inj_hr_wire(self, s, subtle):
        b, r = self.b, self.r
        pid, chk = s["primary_party_id"], s["accounts"][0]
        start, end = self.month_span(s["months"])
        for _ in range(int(r.integers(3, 9))):
            t = self.ts_between(start, end, 9, 16)
            cp = b.cp("HR_ENTITY")
            amt = r.uniform(3000, 4900) if subtle else r.uniform(6000, 40000)
            out = b.chance(0.6)
            self.L(b.add(chk, t, "WIRE_OUT" if out else "WIRE_IN", "DR" if out else "CR", amt, "WIRE",
                         counterparty_id=cp, cp_country=b.cp_country(cp),
                         initiated_by_party_id=pid if out else None, description="International wire"))

    def inj_dormant(self, s, subtle):
        b, r = self.b, self.r
        pid, (dorm, chk) = s["primary_party_id"], s["accounts"]
        wake = s["params"]["wake"]
        total = r.uniform(9000, 14000) if subtle else r.uniform(15000, 80000)
        for part in r.dirichlet(np.ones(int(r.integers(2, 5)))) * total:
            t = self.ts_between(wake, wake + timedelta(days=10), 9, 16)
            if b.chance(0.6):
                cp = b.cp(b.choice(["OFFSHORE", "INDIVIDUAL"]))
                self.L(b.add(dorm, t, "WIRE_IN", "CR", part, "WIRE", counterparty_id=cp,
                             cp_country=b.cp_country(cp), description="Incoming wire"))
            else:
                self.L(b.add(dorm, t, "CASH_DEPOSIT", "CR", min(part, 9500), "BRANCH", is_cash=True,
                             initiated_by_party_id=pid, description="Cash deposit"))
                part = min(part, 9500)
            t2 = t + timedelta(days=int(r.integers(2, 12)))
            self.L2(b.xfer(dorm, chk, t2, part * 0.97, "TRANSFER_OUT", "TRANSFER_IN", pid, "Transfer"))
            cp = b.cp(b.choice(["OFFSHORE", "CRYPTO", "INDIVIDUAL"]))
            self.L(b.add(chk, t2 + timedelta(days=1), "WIRE_OUT", "DR", part * 0.95, "WIRE", counterparty_id=cp,
                         cp_country=b.cp_country(cp), initiated_by_party_id=pid, description="Wire out"))

    def inj_cash_profile(self, s, subtle):
        b, r = self.b, self.r
        pid, chk = s["primary_party_id"], s["accounts"][0]
        for m in s["months"]:
            ms, nd = month_bounds(m)
            total = r.uniform(8000, 12000) if subtle else r.uniform(15000, 40000)
            n = int(r.integers(3, 6) if subtle else r.integers(5, 11))
            for amt in r.dirichlet(np.ones(n) * 4) * total:
                amt = min(amt, 7500)
                t = ts_on(ms + timedelta(days=int(r.integers(0, nd))), r, 9, 17)
                self.L(b.add(chk, t, "CASH_DEPOSIT", "CR", amt, "BRANCH", is_cash=True,
                             initiated_by_party_id=pid, description="Cash deposit"))
            for amt in r.dirichlet(np.ones(int(r.integers(2, 5)))) * total * r.uniform(0.7, 0.9):
                t = ts_on(ms + timedelta(days=int(r.integers(3, nd))), r, 9, 17)
                typ = b.choice(["P2P_OUT", "CHECK_PAID", "WIRE_OUT"])
                cp = b.cp("INDIVIDUAL")
                self.L(b.add(chk, t, typ, "DR", amt, {"P2P_OUT": "P2P", "CHECK_PAID": "CHECK"}.get(typ, "WIRE"),
                             counterparty_id=cp, initiated_by_party_id=pid, description="Outgoing payment"))

    # ----------------------------------------------------------------- cards
    def inj_cc_overpay_refund(self, s, subtle):
        b, r = self.b, self.r
        pid, (card, chk) = s["primary_party_id"], s["accounts"]
        start, end = self.month_span(s["months"])
        for _ in range(int(r.integers(2, 4))):
            t = self.ts_between(start, end - timedelta(days=16), 9, 16)
            excess = r.uniform(1500, 2500) if subtle else r.uniform(3000, 15000)
            pay = excess + b.acct[card]["credit_limit"] * r.uniform(0.1, 0.3)
            if b.chance(0.6):
                self.L(b.add(card, t, "PAYMENT", "CR", pay, "BRANCH", is_cash=True, initiated_by_party_id=pid,
                             description="Card payment (cash at branch)"))
            else:
                self.L(b.add(card, t, "PAYMENT", "CR", pay, "ACH", counterparty_id=b.cp("INDIVIDUAL"),
                             description="Card payment (external)"))
            t2 = t + timedelta(days=int(r.integers(3, 15)))
            if chk and b.chance(0.6):
                self.L2(b.xfer(card, chk, t2, excess, "CREDIT_BALANCE_REFUND", "TRANSFER_IN", pid,
                               "Credit balance refund"))
            else:
                self.L(b.add(card, t2, "CREDIT_BALANCE_REFUND", "DR", excess, "CHECK",
                             counterparty_id=b.self_external(pid), initiated_by_party_id=pid,
                             description="Credit balance refund by check"))

    def inj_cc_cash_adv(self, s, subtle):
        b, r = self.b, self.r
        pid, (card, chk) = s["primary_party_id"], s["accounts"]
        for m in s["months"]:
            ms, nd = month_bounds(m)
            total = 0
            for _ in range(2 if subtle else int(r.integers(3, 7))):
                t = ts_on(ms + timedelta(days=int(r.integers(0, nd - 8))), r, 6, 23)
                amt = 20 * int(r.integers(25, 126))
                self.L(b.add(card, t, "CASH_ADVANCE", "DR", amt, "ATM", is_cash=True, initiated_by_party_id=pid,
                             location_state=b.choice(C.US_STATES), description="Cash advance"))
                total += amt
            t = ts_on(ms + timedelta(days=nd - 3), r, 9, 16)
            self.L(b.add(card, t, "PAYMENT", "CR", total * r.uniform(1.0, 1.1), "BRANCH", is_cash=True,
                         initiated_by_party_id=pid, description="Card payment (cash at branch)"))

    def inj_cc_3p_pay(self, s, subtle):
        b, r = self.b, self.r
        pid, (card, chk) = s["primary_party_id"], s["accounts"]
        for m in s["months"]:
            ms, nd = month_bounds(m)
            total = 0
            for _ in range(2 if subtle else int(r.integers(3, 7))):
                t = ts_on(ms + timedelta(days=int(r.integers(0, nd))), r, 8, 20)
                amt = r.uniform(400, 1200) if subtle else r.uniform(800, 4000)
                self.L(b.add(card, t, "PAYMENT", "CR", amt, "ACH", counterparty_id=b.cp("INDIVIDUAL"),
                             description="Card payment (external)"))
                total += amt
            spent = 0
            while spent < total * 0.9:
                m_ = b.choice(["5732", "5999"])
                amt = r.uniform(300, 1500)
                t = ts_on(ms + timedelta(days=int(r.integers(0, nd))), r, 8, 22)
                self.L(b.add(card, t, "PURCHASE", "DR", amt, "ONLINE", counterparty_id=b.choice(b.merchants[m_]),
                             mcc=m_, initiated_by_party_id=pid, description="Electronics / gift cards"))
                spent += amt

    def inj_cc_hrmcc(self, s, subtle):
        b, r = self.b, self.r
        pid, (card, chk) = s["primary_party_id"], s["accounts"]
        for m in s["months"]:
            ms, nd = month_bounds(m)
            total = r.uniform(3000, 5000) if subtle else r.uniform(6000, 20000)
            spent = 0
            while spent < total:
                m_ = b.choice(sorted(C.HIGH_RISK_MCC))
                amt = b.lognorm(C.MCC[m_][1] * (1 if subtle else 2), 0.6)
                t = ts_on(ms + timedelta(days=int(r.integers(0, nd))), r, 0, 23)
                self.L(b.add(card, t, "PURCHASE", "DR", amt, "ONLINE", counterparty_id=b.choice(b.merchants[m_]),
                             mcc=m_, initiated_by_party_id=pid, description=C.MCC[m_][0]))
                spent += amt
            for part in r.dirichlet(np.ones(int(r.integers(2, 5)))) * spent:
                t = ts_on(ms + timedelta(days=int(r.integers(5, nd))), r, 9, 16)
                self.L(b.add(card, t, "PAYMENT", "CR", part, "BRANCH", is_cash=True, initiated_by_party_id=pid,
                             description="Card payment (cash at branch)"))

    def inj_cc_bustout(self, s, subtle):
        b, r = self.b, self.r
        pid, (card, chk) = s["primary_party_id"], s["accounts"]
        lim, t0 = b.acct[card]["credit_limit"], s["params"]["bust_start"]

        def max_out(start, target):
            spent = 0
            while spent < target:
                if b.chance(0.25):
                    amt = 20 * int(r.integers(10, 40))
                    tid = b.add(card, ts_on(start + timedelta(days=int(r.integers(0, 8))), r, 6, 23), "CASH_ADVANCE",
                                "DR", amt, "ATM", is_cash=True, initiated_by_party_id=pid, description="Cash advance")
                else:
                    m_ = b.choice(["5732", "5311", "5999"])
                    amt = r.uniform(200, 1800)
                    tid = b.add(card, ts_on(start + timedelta(days=int(r.integers(0, 8))), r, 8, 22), "PURCHASE",
                                "DR", amt, "POS", counterparty_id=b.choice(b.merchants[m_]), mcc=m_,
                                initiated_by_party_id=pid, description="Purchase")
                self.L(tid)
                spent += amt
        max_out(t0, lim * (0.9 if subtle else 1.0))
        if not subtle:
            t_pay = t0 + timedelta(days=9)
            self.L(b.add(card, ts_on(t_pay, r), "PAYMENT", "CR", lim * 0.95, "ACH",
                         counterparty_id=b.self_external(pid), description="Card payment (external)"))
            max_out(t_pay + timedelta(days=1), lim * 0.9)
            self.L(b.add(card, ts_on(t_pay + timedelta(days=5), r), "RETURNED_PAYMENT", "DR", lim * 0.95, "ACH",
                         counterparty_id=b.self_external(pid), description="Payment returned - NSF"))
            b.acct[card]["status"] = "DELINQUENT"

    # ----------------------------------------------------------------- loans
    def inj_ln_early_payoff(self, s, subtle):
        b, r = self.b, self.r
        pid, (loan, chk) = s["primary_party_id"], s["accounts"]
        a, payoff, fund = b.acct[loan], s["params"]["payoff"], s["params"]["funding"]
        months_in = max(1, (payoff.date() - a["open_date"]).days // 30)
        remaining = a["loan_amount"] * max(0.5, 1 - months_in / a["loan_term_months"] * 1.2)
        amt = remaining * (r.uniform(0.3, 0.4) if subtle else 1.0)
        if not subtle:
            b.acct[loan]["status"] = "PAID_OFF"
        if fund == "CASH":
            left = amt
            while left > 0:
                part = min(left, r.uniform(3000, 9000))
                t = payoff - timedelta(days=int(r.integers(1, 14)), hours=int(r.integers(1, 6)))
                self.L(b.add(chk, t, "CASH_DEPOSIT", "CR", part, "BRANCH", is_cash=True, initiated_by_party_id=pid,
                             location_state=b.choice(C.US_STATES), description="Cash deposit"))
                left -= part
            self.L2(b.xfer(chk, loan, payoff, amt, "LOAN_PAYMENT_OUT", "PAYOFF" if not subtle else "EXTRA_PAYMENT",
                           pid, "Loan payoff"))
        elif fund == "OFFSHORE_WIRE":
            cp = b.cp(b.choice(["OFFSHORE", "HR_ENTITY"]))
            self.L(b.add(chk, payoff - timedelta(days=2), "WIRE_IN", "CR", amt * 1.02, "WIRE", counterparty_id=cp,
                         cp_country=b.cp_country(cp), description="Incoming wire"))
            self.L2(b.xfer(chk, loan, payoff, amt, "LOAN_PAYMENT_OUT", "PAYOFF" if not subtle else "EXTRA_PAYMENT",
                           pid, "Loan payoff"))
        else:
            self.L(b.add(loan, payoff, "PAYOFF" if not subtle else "EXTRA_PAYMENT", "CR", amt, "ACH",
                         counterparty_id=b.cp("INDIVIDUAL"), description="Loan payoff (third party)"))

    def inj_ln_rapid_out(self, s, subtle):
        b, r = self.b, self.r
        pid, (loan, chk) = s["primary_party_id"], s["accounts"]
        a = b.acct[loan]
        t = ts_on(_dt(a["open_date"]), r, 9, 16)
        self.L2(b.xfer(loan, chk, t, a["loan_amount"], "DISBURSEMENT", "LOAN_DISBURSEMENT_IN", pid,
                       "Personal loan proceeds"))
        out = a["loan_amount"] * (r.uniform(0.6, 0.75) if subtle else r.uniform(0.75, 1.0))
        for part in r.dirichlet(np.ones(int(r.integers(1, 4)))) * out:
            t2 = t + timedelta(days=int(r.integers(1, 7)), hours=int(r.integers(0, 5)))
            if subtle:
                cp = b.cp("INDIVIDUAL")
                self.L(b.add(chk, t2, "P2P_OUT" if part < 5000 else "WIRE_OUT", "DR", part,
                             "P2P" if part < 5000 else "WIRE", counterparty_id=cp, initiated_by_party_id=pid,
                             description="Outgoing transfer"))
            else:
                cp = b.cp(b.choice(["HR_ENTITY", "OFFSHORE", "CRYPTO"]))
                self.L(b.add(chk, t2, "WIRE_OUT", "DR", part, "WIRE", counterparty_id=cp,
                             cp_country=b.cp_country(cp), initiated_by_party_id=pid, description="Outgoing wire"))

    def inj_ln_3p_repay(self, s, subtle):
        b, r = self.b, self.r
        pid, loan = s["primary_party_id"], s["accounts"][0]
        pmt = b.acct[loan]["monthly_payment"]
        payors = [b.cp("INDIVIDUAL") for _ in range(int(r.integers(2, 5)))]
        for m in s["months"]:
            ms, nd = month_bounds(m)
            for part in r.dirichlet(np.ones(1 if subtle else int(r.integers(2, 4)))) * pmt * r.uniform(1.0, 2.5):
                t = ts_on(ms + timedelta(days=int(r.integers(1, 20))), r, 9, 17)
                if b.chance(0.3):
                    self.L(b.add(loan, t, "PAYMENT", "CR", part, "BRANCH", is_cash=True,
                                 description="Loan payment (cash at branch)"))
                else:
                    self.L(b.add(loan, t, "PAYMENT", "CR", part, "ACH", counterparty_id=b.choice(payors),
                                 description="Loan payment (external)"))

    # --------------------------------------------------------- cross-product
    def inj_xp_cash_to_credit(self, s, subtle):
        b, r = self.b, self.r
        pid = s["primary_party_id"]
        card, chk, loan = s["accounts"][0], s["accounts"][1], s["params"].get("loan")
        start, end = self.month_span(s["months"])
        n_cycles = int(r.integers(2, 4))
        for i in range(n_cycles):
            c0 = start + timedelta(days=int(i * (end - start).days / n_cycles))
            spend = r.uniform(6000, 9000) if subtle else r.uniform(10000, 22000)
            spent = 0
            while spent < spend:
                m_ = b.choice(["5311", "5732", "4511", "7011"])
                amt = r.uniform(400, 3000)
                self.L(b.add(card, ts_on(c0 + timedelta(days=int(r.integers(0, 10))), r, 8, 22), "PURCHASE", "DR",
                             amt, "POS", counterparty_id=b.choice(b.merchants[m_]), mcc=m_,
                             initiated_by_party_id=pid, description="Purchase"))
                spent += amt
            cash = 0
            for _ in range(int(r.integers(3, 5) if subtle else r.integers(4, 9))):
                amt = r.uniform(1500, 2500) if subtle else r.uniform(2000, 6000)
                self.L(b.add(chk, ts_on(c0 + timedelta(days=int(r.integers(8, 18))), r, 9, 17), "CASH_DEPOSIT",
                             "CR", amt, "BRANCH", is_cash=True, initiated_by_party_id=pid,
                             location_state=b.choice(C.US_STATES), description="Cash deposit"))
                cash += amt
            t = ts_on(c0 + timedelta(days=int(r.integers(19, 24))), r, 9, 17)
            pay = cash * r.uniform(0.75, 0.95)
            if loan and b.chance(0.4):
                self.L2(b.xfer(chk, loan, t, pay * 0.4, "LOAN_PAYMENT_OUT", "EXTRA_PAYMENT", pid, "Extra principal"))
                pay *= 0.6
            self.L2(b.xfer(chk, card, t, pay, "CARD_PAYMENT_OUT", "PAYMENT", pid, "Card payment"))

    def inj_joint_bad_actor(self, s, subtle):
        b, r = self.b, self.r
        bad, chk = s["params"]["bad_actor"], s["accounts"][0]
        start, end = self.month_span(s["months"])
        week = start
        while week + timedelta(days=7) <= end:
            total = 0
            for _ in range(2 if subtle else int(r.integers(3, 5))):
                t = ts_on(week + timedelta(days=int(r.integers(0, 6))), r, 9, 17)
                amt = r.uniform(7500, 9900)
                self.L(b.add(chk, t, "CASH_DEPOSIT", "CR", amt, "BRANCH", is_cash=True, initiated_by_party_id=bad,
                             location_state=b.choice(C.US_STATES), description="Cash deposit"))
                total += amt
            cp = b.cp(b.choice(["OFFSHORE", "HR_ENTITY", "INDIVIDUAL"]))
            self.L(b.add(chk, ts_on(week + timedelta(days=int(r.integers(6, 9))), r, 9, 16), "WIRE_OUT", "DR",
                         total * r.uniform(0.85, 0.95), "WIRE", counterparty_id=cp, cp_country=b.cp_country(cp),
                         initiated_by_party_id=bad, description="Wire out"))
            week += timedelta(days=int(r.integers(9, 16)))

    def inj_thin_spread(self, s, subtle):
        b, r = self.b, self.r
        pid = s["primary_party_id"]
        chk, sav, card = s["accounts"][:3]
        loan = s["params"].get("loan")
        for m in s["months"]:
            ms, nd = month_bounds(m)
            D = lambda lo=0, hi=nd: ms + timedelta(days=int(r.integers(lo, hi)))
            for acct, n in ((chk, int(r.integers(2, 4))), (sav, int(r.integers(1, 3)))):
                for _ in range(n):
                    self.L(b.add(acct, ts_on(D(), r, 9, 17), "CASH_DEPOSIT", "CR", r.uniform(2500, 4000), "BRANCH",
                                 is_cash=True, initiated_by_party_id=pid, description="Cash deposit"))
            for _ in range(int(r.integers(1, 3))):
                self.L(b.add(card, ts_on(D(), r, 9, 17), "PAYMENT", "CR", r.uniform(1500, 3000), "BRANCH",
                             is_cash=True, initiated_by_party_id=pid, description="Card payment (cash at branch)"))
            if loan:
                self.L(b.add(loan, ts_on(D(), r, 9, 17), "EXTRA_PAYMENT", "CR", r.uniform(1500, 3000), "BRANCH",
                             is_cash=True, initiated_by_party_id=pid, description="Extra principal (cash)"))
            self.L2(b.xfer(chk, card, ts_on(D(15), r), r.uniform(2000, 4000), "CARD_PAYMENT_OUT", "PAYMENT", pid,
                           "Card payment"))
            spent = 0
            while spent < r.uniform(6000, 9000):
                m_ = b.choice(["5732", "5311", "5999"])
                amt = r.uniform(300, 1500)
                self.L(b.add(card, ts_on(D(), r, 8, 22), "PURCHASE", "DR", amt, "POS",
                             counterparty_id=b.choice(b.merchants[m_]), mcc=m_, initiated_by_party_id=pid,
                             description="Purchase"))
                spent += amt
            for _ in range(int(r.integers(1, 3))):
                cp = b.cp("INDIVIDUAL")
                b_amt = r.uniform(2000, 5000)
                self.L(b.add(chk, ts_on(D(5), r), "P2P_OUT", "DR", b_amt, "P2P", counterparty_id=cp,
                             initiated_by_party_id=pid, description="Zelle"))


# =============================================================================
# LEGITIMATE SPIKES (false-positive fodder - NOT labelled suspicious)
# =============================================================================
SPIKES = ["BONUS", "CAR_SALE", "INHERITANCE", "HOME_PURCHASE", "TRAVEL_ABROAD", "LARGE_CASH_LEGIT",
          "REFINANCE_PAYOFF"]


def plan_spikes(bank, used):
    b, r = bank, bank.rng
    for pid in b.primary_ids:
        if pid in used or not b.chance(C.P_LEGIT_SPIKE):
            continue
        p, acc = b.party[pid], b.primary_accts[pid]
        ok = []
        if acc.get("CHECKING"):
            ok += ["INHERITANCE"]
            if p["segment"] in ("MASS", "MASS_AFFLUENT", "AFFLUENT"):
                ok += ["BONUS"]
            ok += ["CAR_SALE"]
            if acc.get("SAVINGS") and not acc.get("MORTGAGE") and p["segment"] != "STUDENT":
                ok += ["HOME_PURCHASE"]
            if p["is_cash_intensive"]:
                ok += ["LARGE_CASH_LEGIT"] * 3
        if acc.get("CREDIT_CARD"):
            ok += ["TRAVEL_ABROAD"]
        if acc.get("MORTGAGE") or acc.get("AUTO_LOAN"):
            ok += ["REFINANCE_PAYOFF"]
        if not ok:
            continue
        kind = b.choice(ok)
        when = datetime(2026, int(r.integers(4, 10)), int(r.integers(1, 22)), 11)
        loan = None
        if kind == "CAR_SALE" and acc.get("AUTO_LOAN"):
            loan = acc["AUTO_LOAN"][0]
        elif kind == "REFINANCE_PAYOFF":
            loan = (acc.get("MORTGAGE") or acc.get("AUTO_LOAN"))[0]
        if loan:
            if b.acct[loan]["open_date"] >= when.date() - timedelta(days=60):
                continue   # loan too new to be refinanced / paid off
            b.loan_closed_on[loan] = when + timedelta(days=5)
        b.spikes.append(dict(party_id=pid, spike_type=kind, when=when, loan=loan))


def inject_spikes(bank):
    b, r = bank, bank.rng
    for sp in b.spikes:
        pid, kind, t, loan = sp["party_id"], sp["spike_type"], sp["when"], sp["loan"]
        chk, sav, card = b.first(pid, "CHECKING"), b.first(pid, "SAVINGS"), b.first(pid, "CREDIT_CARD")
        if kind == "BONUS":
            b.add(chk, t, "ACH_CREDIT", "CR", b.party[pid]["annual_income"] * r.uniform(0.08, 0.25), "ACH",
                  counterparty_id=b.cp("EMPLOYER"), description="Annual bonus")
        elif kind == "CAR_SALE":
            amt = r.uniform(9000, 35000)
            b.add(chk, t, "CHECK_DEPOSIT", "CR", amt, "BRANCH", counterparty_id=b.cp("INDIVIDUAL"),
                  description="Vehicle sale proceeds")
            if loan:
                a = b.acct[loan]
                b.xfer(chk, loan, t + timedelta(days=4), min(amt * 0.9, a["loan_amount"] * 0.6), "LOAN_PAYMENT_OUT",
                       "PAYOFF", pid, "Auto loan payoff - vehicle sold")
                a["status"] = "PAID_OFF"
        elif kind == "INHERITANCE":
            amt = r.uniform(40000, 250000)
            b.add(chk, t, "WIRE_IN", "CR", amt, "WIRE", counterparty_id=b.cp("LAW_FIRM"),
                  description="Estate distribution")
            b.add(chk, t + timedelta(days=int(r.integers(2, 20))), "WIRE_OUT", "DR", amt * r.uniform(0.5, 0.9),
                  "WIRE", counterparty_id=b.cp("BROKERAGE"), initiated_by_party_id=pid, description="Wire to brokerage")
        elif kind == "HOME_PURCHASE":
            amt = r.uniform(30000, 150000)
            b.xfer(sav, chk, t, amt, "TRANSFER_OUT", "TRANSFER_IN", pid, "Down payment funds")
            b.add(chk, t + timedelta(days=1), "WIRE_OUT", "DR", amt, "WIRE", counterparty_id=b.cp("TITLE_CO"),
                  initiated_by_party_id=pid, description="Home purchase - closing funds")
        elif kind == "TRAVEL_ABROAD":
            ctry = b.choice(list(b.foreign_merchants))
            for _ in range(int(r.integers(8, 25))):
                b.add(card, ts_on(t + timedelta(days=int(r.integers(0, 12))), r, 7, 23), "PURCHASE", "DR",
                      b.lognorm(90, 0.8), "POS", counterparty_id=b.choice(b.foreign_merchants[ctry]), cp_country=ctry,
                      mcc="5812", initiated_by_party_id=pid, description=f"Purchase abroad ({ctry})")
        elif kind == "LARGE_CASH_LEGIT":
            b.add(chk, t, "CASH_DEPOSIT", "CR", r.uniform(10500, 25000), "BRANCH", is_cash=True,
                  initiated_by_party_id=pid, description="Business cash takings (CTR filed)")
        elif kind == "REFINANCE_PAYOFF":
            a = b.acct[loan]
            b.add(loan, t + timedelta(days=4), "PAYOFF", "CR", a["loan_amount"] * r.uniform(0.6, 0.9), "WIRE",
                  counterparty_id=b.cp("LENDER"), description="Payoff - refinanced with another lender")
            a["status"] = "PAID_OFF"


# =============================================================================
# LEGITIMATE LOOK-ALIKES - ongoing customer behaviour that trips rules but isn't laundering.
# This is what makes real rule-based monitoring noisy (and what the ML layer must learn to
# down-weight). Recorded in legit_spikes with spike_type = pattern name; never labelled suspicious.
# =============================================================================
def inject_lookalikes(bank, used):
    b, r = bank, bank.rng
    pool = [p for p in b.primary_ids if p not in used]

    def pick(pred, share=None, n=None):
        c = [p for p in pool if pred(b.party[p], b.primary_accts[p])]
        k = n if n is not None else int(round(len(c) * share))
        sel = list(r.choice(c, size=min(k, len(c)), replace=False)) if c else []
        return [str(x) for x in sel]

    def rec(pid, kind):
        b.spikes.append(dict(party_id=pid, spike_type=kind, when=None, loan=None))

    # 1) cash businesses banking takings 3x a week in amounts that overlap the structuring band
    for pid in pick(lambda p, a: p["is_cash_intensive"] and a.get("CHECKING"), share=0.06):
        chk = b.first(pid, "CHECKING"); rec(pid, "CASH_BUSINESS_FREQUENT_BANKING")
        b.party[pid]["expected_monthly_cash"] = 100000
        d = C.WINDOW_START
        while d < C.WINDOW_END:
            for off in (0, 2, 4):
                b.add(chk, ts_on(d + timedelta(days=off), r, 15, 18), "CASH_DEPOSIT", "CR", r.uniform(7000, 10800),
                      "BRANCH", is_cash=True, initiated_by_party_id=pid, description="Business cash takings")
            d += timedelta(days=7)
    # 2) affluent investors moving large sums brokerage -> title company / other brokerage
    for pid in pick(lambda p, a: p["segment"] == "AFFLUENT" and a.get("CHECKING"), share=0.18):
        chk = b.first(pid, "CHECKING"); rec(pid, "INVESTMENT_MOVEMENT")
        for _ in range(int(r.integers(1, 3))):
            t = datetime(2026, int(r.integers(4, 10)), int(r.integers(1, 25)), 10)
            amt = r.uniform(25000, 150000)
            b.add(chk, t, "WIRE_IN", "CR", amt, "WIRE", counterparty_id=b.cp("BROKERAGE"),
                  description="Wire from brokerage")
            b.add(chk, t + timedelta(days=int(r.integers(1, 3))), "WIRE_OUT", "DR", amt * r.uniform(0.85, 1.0),
                  "WIRE", counterparty_id=b.cp(b.choice(["TITLE_CO", "LAW_FIRM", "BROKERAGE"])),
                  initiated_by_party_id=pid, description="Investment / property settlement")
    # 3) diaspora family support to high-risk-listed countries (legit, some >= $5k)
    for pid in pick(lambda p, a: p["segment"] in ("MASS", "MASS_AFFLUENT", "SELF_EMPLOYED") and a.get("CHECKING"),
                    n=25):
        chk = b.first(pid, "CHECKING"); rec(pid, "DIASPORA_SUPPORT")
        cp = b.choice([c for c in b.cp_pool["HR_ENTITY"] if b.cp_country(c) in ("VE", "HT")] or b.cp_pool["HR_ENTITY"])
        for m in C.MONTHS:
            if b.chance(0.7):
                ms, nd = month_bounds(m)
                b.add(chk, ts_on(ms + timedelta(days=int(r.integers(0, nd))), r, 9, 16), "WIRE_OUT", "DR",
                      b.lognorm(2500, 0.7), "WIRE", counterparty_id=cp, cp_country=b.cp_country(cp),
                      initiated_by_party_id=pid, description="Family support")
    # 4) marketplace sellers / side hustles: many small P2P in from strangers, paying suppliers out
    for pid in pick(lambda p, a: p["segment"] in ("MASS", "STUDENT", "SELF_EMPLOYED") and a.get("CHECKING"),
                    share=0.025):
        chk = b.first(pid, "CHECKING"); rec(pid, "MARKETPLACE_SELLER")
        supplier = b.cp("EMPLOYER")
        for m in C.MONTHS:
            ms, nd = month_bounds(m)
            tot = 0
            for _ in range(int(r.integers(8, 30))):
                amt = b.lognorm(350, 0.7)
                b.add(chk, ts_on(ms + timedelta(days=int(r.integers(0, nd))), r, 8, 22), "P2P_IN", "CR", amt, "P2P",
                      counterparty_id=b.cp("INDIVIDUAL"), description="Zelle - marketplace sale")
                tot += amt
            b.add(chk, ts_on(ms + timedelta(days=nd - 2), r, 9, 16), b.choice(["WIRE_OUT", "P2P_OUT", "CHECK_PAID"]),
                  "DR", tot * r.uniform(0.6, 0.9), "WIRE", counterparty_id=supplier, initiated_by_party_id=pid,
                  description="Supplier payment")
    # 5) retirees whose adult children help pay the card
    for pid in pick(lambda p, a: p["segment"] == "RETIREE" and a.get("CREDIT_CARD"), share=0.12):
        card = b.first(pid, "CREDIT_CARD"); rec(pid, "FAMILY_PAYS_CARD")
        kids = [b.cp("INDIVIDUAL") for _ in range(2)]
        for m in C.MONTHS:
            ms, nd = month_bounds(m)
            for k in kids:
                if b.chance(0.6):
                    b.add(card, ts_on(ms + timedelta(days=int(r.integers(0, nd))), r), "PAYMENT", "CR",
                          r.uniform(400, 1500), "ACH", counterparty_id=k, description="Card payment (external)")
    # 6) borrowers who pay their loan in cash, twice a month
    for pid in pick(lambda p, a: any(a.get(s) for s in ("PERSONAL_LOAN", "AUTO_LOAN")), share=0.04):
        loan = b.loans_of(pid)[0]; rec(pid, "CASH_LOAN_PAYER")
        for m in C.MONTHS:
            ms, nd = month_bounds(m)
            for d in (3, 17):
                b.add(loan, ts_on(ms + timedelta(days=d), r, 9, 17), "PAYMENT", "CR",
                      b.acct[loan]["monthly_payment"] / 2, "BRANCH", is_cash=True, initiated_by_party_id=pid,
                      description="Loan payment (cash at branch)")
    # 7) big purchase returned after the bill was paid -> legit credit-balance refund
    for pid in pick(lambda p, a: a.get("CREDIT_CARD") and p["segment"] in ("MASS_AFFLUENT", "AFFLUENT"),
                    share=0.03):
        card = b.first(pid, "CREDIT_CARD"); rec(pid, "RETURN_AFTER_PAYMENT")
        t = datetime(2026, int(r.integers(4, 10)), int(r.integers(1, 15)), 13)
        amt = r.uniform(1600, 4500)
        b.add(card, t, "MERCHANT_REFUND", "CR", amt, "ONLINE", counterparty_id=b.choice(b.merchants["5732"]),
              mcc="5732", description="Merchant refund - returned TV")
        b.add(card, t + timedelta(days=int(r.integers(5, 12))), "CREDIT_BALANCE_REFUND", "DR", amt * 0.95, "CHECK",
              counterparty_id=b.self_external(pid), initiated_by_party_id=pid, description="Credit balance refund")
    # 8) cash-reliant card users taking several small cash advances
    for pid in pick(lambda p, a: a.get("CREDIT_CARD") and p["segment"] in ("MASS", "SELF_EMPLOYED"), share=0.03):
        card = b.first(pid, "CREDIT_CARD"); rec(pid, "FREQUENT_CASH_ADVANCE")
        for m in C.MONTHS:
            ms, nd = month_bounds(m)
            tot = 0
            for _ in range(int(r.integers(2, 6))):
                amt = 20 * int(r.integers(15, 45))
                b.add(card, ts_on(ms + timedelta(days=int(r.integers(0, 20))), r, 6, 23), "CASH_ADVANCE", "DR", amt,
                      "ATM", is_cash=True, initiated_by_party_id=pid, description="Cash advance")
                tot += amt
            b.add(card, ts_on(ms + timedelta(days=24), r), "PAYMENT", "CR", tot, "ACH",
                  counterparty_id=b.self_external(pid), description="Card payment (external)")
    # 9) crypto / gambling hobbyists funding their card normally
    for pid in pick(lambda p, a: a.get("CREDIT_CARD") and p["segment"] in ("MASS_AFFLUENT", "AFFLUENT"), share=0.03):
        card = b.first(pid, "CREDIT_CARD"); rec(pid, "CRYPTO_INVESTOR")
        b.acct[card]["credit_limit"] = max(b.acct[card]["credit_limit"], 20000)
        for m in C.MONTHS:
            if b.chance(0.6):
                ms, nd = month_bounds(m)
                for _ in range(int(r.integers(3, 10))):
                    m_ = b.choice(sorted(C.HIGH_RISK_MCC))
                    b.add(card, ts_on(ms + timedelta(days=int(r.integers(0, nd))), r, 0, 23), "PURCHASE", "DR",
                          b.lognorm(700, 0.6), "ONLINE", counterparty_id=b.choice(b.merchants[m_]), mcc=m_,
                          initiated_by_party_id=pid, description=C.MCC[m_][0])
