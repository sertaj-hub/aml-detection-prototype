# Baseline 001 — Detection rules (AS-IS)

| | |
|---|---|
| Status | AS-IS (reverse-engineered; awaiting SME review) |
| Code | `rules_engine.py` — `RULES` catalog, `RulesEngine.r_*` methods |

## 1. Common mechanics

**Event points** = `base_points × severity`, rounded to 0.1, where
`severity = clamp(1 + 0.25 × (ratio − 1), 1.0, 1.5)`. `ratio` is a rule-specific "how far above threshold"
number (column *Severity ratio* below). Severity is 1.0 at threshold and 1.5 at ≥3× threshold.

**Trigger transactions** share the event's points pro-rata by `amount`. CONTEXT transactions carry 0.

**Cycle** = month of the event's `fire_ts` (stated per rule). Windows spanning month-end therefore belong to the later month.

**Window semantics**: `greedy_windows(df, days, cond)` walks time-sorted rows; for each start row it takes all
rows with `txn_ts ≤ start + days` (**inclusive**), and if `cond` holds emits the group and resumes after it.
Episodes are therefore non-overlapping per account, and a start row is only tried once.

**Scope**: `DEPOSIT` rules use `product_type == DEPOSIT`; events attach to the account, and through it to the
account's primary party.

**Units**: amounts are plain dollars; no currency conversion.

## 2. Rule table

| ID | Name | Base pts | Trigger condition (as coded) | Window / grouping | Fires at | Severity ratio |
|---|---|---|---|---|---|---|
| R-DEP-01 | Structuring | 30 | ≥3 `CASH_DEPOSIT`s with 8,000 ≤ amount ≤ 9,999.99 | rolling 7 days (inclusive), per account, greedy | last txn in window | n ÷ 3 |
| R-DEP-02 | Rapid movement | 25 | external credits (types WIRE_IN, ACH_CREDIT, CHECK_DEPOSIT, CASH_DEPOSIT, P2P_IN; each ≥1,000) summing ≥20,000 in 3 days, and debits (any DR ≥500) from window start to window end + 3 days ≥80% of the credits | 3-day greedy credit window, per account | last txn of credits+debits | min(credits ÷ 20,000, (out÷in) ÷ 0.8 × 2) |
| R-DEP-03 | High-risk geography wire | 20 | wire (IN/OUT) with `cp_country` in `HIGH_RISK_COUNTRIES`: any one ≥5,000, **or** ≥3 such wires of any size | calendar month, per account | last HR wire in month | max(largest ÷ 5,000, count ÷ 3) |
| R-DEP-04 | Dormant reactivation | 15 | gap ≥180 days since previous non-SYSTEM activity (or `last_activity_pre_window` for the first txn), then credits (any CR) ≥10,000 within 30 days of the reactivating txn | first reactivation per account only | last txn in the 30 days | credits ÷ 10,000 |
| R-DEP-05 | Cash inconsistent with profile | 20 | monthly cash deposits ≥10,000 **and** ≥3 × expected monthly cash (expected floored at 100) | calendar month, per account | last cash deposit in month | min(total ÷ 10,000, total ÷ (3 × expected)) |
| R-DEP-06 | Funnel / many-to-one | 30 | in a calendar month: ≥6 inbound credits from ≥4 distinct sources totalling ≥8,000, **and** outflows ≥70% of inbound. Inbound = P2P_IN/ACH_CREDIT from INDIVIDUAL counterparties, or P2P_IN from an internal account, or CASH_DEPOSIT with a counterparty or made outside the account's branch state. Outflows = DR excluding POS_PURCHASE, ACH_DEBIT, INTEREST, TRANSFER_OUT, CARD_PAYMENT_OUT, LOAN_PAYMENT_OUT | calendar month, per account | latest of inbound/outflow | min(sources ÷ 4, total ÷ 8,000) |
| R-CRD-01 | Cash-advance cycling | 20 | ≥3 cash advances totalling ≥2,500 in a month, and card payments in the **same month** ≥80% of the advances | calendar month | latest advance/payment | total ÷ 2,500 |
| R-CRD-02 | Overpayment + credit-balance refund | 22 | any `CREDIT_BALANCE_REFUND` ≥1,500 (payments in the prior 30 days are attached as evidence but **not required**) | per refund | refund time | refund ÷ 1,500 |
| R-CRD-03 | Third-party card payments | 15 | card `PAYMENT`s from third parties: ≥2 distinct payors and ≥2,000 total in a month | calendar month | last payment | max(payors ÷ 2, total ÷ 2,000) |
| R-CRD-04 | High-risk MCC spend | 15 | `PURCHASE`s with MCC 7995/6051 totalling ≥3,000 **and** ≥30% of the month's purchases | calendar month | last HR purchase | total ÷ 3,000 |
| R-CRD-05 | Bust-out | 20 | (a) any `RETURNED_PAYMENT`; **or** (b) for accounts with no returned payment: purchases + cash advances in 14 days ≥85% of credit limit **and** ≥3 × the account's median monthly draw in other months (floor 1) | (a) per returned payment; (b) 14-day greedy | (a) return time; (b) last draw | (a) fixed 2.0 (→ severity 1.25); (b) total ÷ (0.85 × limit) |
| R-LN-01 | Early payoff / lump sum | 18 | loan `PAYOFF` or `EXTRA_PAYMENT` with account age ≤24 months (days ÷ 30.4) and (amount ≥10,000 **or** ≥25% of original loan amount) | per payment | payment time | max(amount ÷ 10,000, amount ÷ 25% of loan) |
| R-LN-02 | Loan proceeds moved out | 25 | `LOAN_DISBURSEMENT_IN` to a deposit account, then WIRE_OUT/P2P_OUT/CHECK_PAID/ATM_WITHDRAWAL from that account within 7 days (exclusive of the disbursement time) ≥70% of proceeds | per disbursement | last outflow; attached to the **loan** account | (out÷proceeds) ÷ 0.7, ×1.3 if any outflow has non-US country |
| R-LN-03 | Third-party / cash loan repayments | 12 | ≥2 loan `PAYMENT`/`EXTRA_PAYMENT`s in a month that are cash or from a third party | calendar month | last payment | count ÷ 2 |
| R-XP-01 | Cash-funded credit paydown | 25 | party cash-in (cash `CASH_DEPOSIT`, card `PAYMENT`, loan `PAYMENT`/`EXTRA_PAYMENT`/`PAYOFF` with `is_cash`) summing ≥ max(10,000, 2 × expected cash) in 21 days, and card `PAYMENT`s + loan `EXTRA_PAYMENT`/`PAYOFF` from window start to end + 7 days ≥50% of the cash | per primary party, 21-day greedy | last trigger txn; attached to the most-paid-down account; product `CROSS_PRODUCT` | min(cash ÷ 10,000, (paydown÷cash) × 2) |

Point range per event: e.g. R-DEP-01 30–45; R-LN-03 12–18; R-DEP-04 15–22.5. With `ALERT_THRESHOLD = 35`,
a single event can alert on its own only if `base × severity ≥ 35` (R-DEP-01 at ratio ≥1.67 i.e. 5+ deposits;
see 003 for other combinations).

## 3. Open questions raised by reading the code

These are observations, not decisions. Each needs an SME call: *keep as is*, *change (new spec)*, or *document as intended*.

- **Q-01 Thresholds are literals.** All rule amounts/windows are hard-coded in methods, so a threshold change is a code change, with no per-segment thresholds.
- **Q-02 R-CRD-02 does not check for overpayment.** The catalog says "issued after card overpayment", but any credit-balance refund ≥1,500 fires, even with no payments in the prior 30 days (the context list is then just the refund).
- **Q-03 R-CRD-05(a) fires on any single returned payment** with a fixed ratio of 2.0 (severity 1.25, 25 pts). Returned payments are common for ordinary customers; no amount, frequency or utilisation condition.
- **Q-04 R-DEP-02 debits are broad.** "Out" counts every debit ≥500, including POS, ACH debits and internal transfers, and the debit window starts at the *first credit*, so debits made before later credits in the window count.
- **Q-05 R-DEP-04 credits are broad.** Any customer-channel `CR` counts (including internal transfers) and only the first reactivation per account in the whole window is evaluated.
- **Q-06 R-XP-01 double-counts cash card/loan payments.** A cash card payment is both "cash in" and part of "paydown", so cash paying a card can satisfy both sides alone. Paydown also counts non-cash card payments.
- **Q-07 R-LN-01 uses the same $10,000 floor for every loan type.** Mortgages and auto loans routinely have lump sums; 25% of mortgage principal is a very high bar, but $10,000 is not.
- **Q-08 R-LN-02 may hit legitimate loan uses** (e.g. auto/mortgage proceeds paid out to a dealer or seller by wire/cheque).
- **Q-09 Calendar-month rules split activity at month-end.** R-DEP-03/05/06, R-CRD-01/03/04, R-LN-03 evaluate a calendar month; the same behaviour straddling 31st/1st may not fire.
- **Q-10 R-DEP-06 inbound definition is narrow** (INDIVIDUAL counterparties, internal P2P, out-of-state cash). Wire senders and business payers are excluded.
- **Q-11 Joint / secondary holders.** Events attach to the primary party even when `initiated_by_party_id` is a secondary holder; rules do not use `initiated_by_party_id` at all.
- **Q-12 Window boundaries are inclusive.** For R-DEP-01 a deposit exactly 7×24h after the first is inside the window; there are no tests pinning this.
- **Q-13 Severity ratios are heterogeneous.** Some use counts, some amounts, some a product (R-DEP-02, R-XP-01). Equal "1.5×" severities are not comparable across rules.
- **Q-14 R-DEP-01 band top.** Deposits of exactly 10,000 are outside the structuring band (8,000–9,999.99); deposits of 7,900 are also outside it.
