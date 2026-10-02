# Baseline 002 — ML anomaly events (AS-IS)

| | |
|---|---|
| Status | AS-IS (reverse-engineered; awaiting SME review) |
| Code | `ml_events.py` — `MLEventBuilder`, `FEATURES`, `MODELS` |

## 1. Models

| Model ID | Name | Rows scored |
|---|---|---|
| M-ANOM-DEP | Deposit behaviour anomaly | account-months with ≥1 non-SYSTEM deposit transaction |
| M-ANOM-CRD | Card behaviour anomaly | account-months with ≥1 card transaction |
| M-ANOM-LN | Loan repayment anomaly | account-months with ≥1 loan-ledger transaction |

One IsolationForest per product: `n_estimators=300`, `random_state=42`, other parameters default
(`contamination='auto'`, `max_samples='auto'`). **Fit and scored on the same rows, all six months together.**
Labels are not used. Accounts/months with no transactions have no row and are never scored.

## 2. Features (account-month)

Features compare behaviour with the party's own KYC profile where possible. Money features in `LOG_FEATURES`
are `log1p`-transformed before fitting; `±inf` → 0, NaN → 0.

**Deposit (13)**: `f_credits_vs_expected` (external credits ÷ expected monthly credits, floor 500) ·
`f_cash_in_vs_expected` (cash ÷ expected cash, floor 100) · `f_cash_in_amt` · `f_n_cash_band` (cash deposits 8,000–9,999.99) ·
`f_n_cash_states` · `f_n_inbound_sources` · `f_pass_through` (non-routine debits ÷ external credits, floor 1,000, cap 3) ·
`f_wire_in_amt` · `f_wire_out_amt` · `f_intl_wire_amt` · `f_hr_wire_amt` · `f_max_credit_vs_expected` · `f_dormancy_days`.

**Card (12)**: `f_purchase_vs_limit` · `f_spend_vs_usual` (÷ account median purchase month, floor 50) · `f_cash_adv_amt` · `f_n_cash_adv` ·
`f_payments_vs_limit` · `f_cash_payment_amt` · `f_third_party_pay_amt` · `f_n_third_party_payors` · `f_hr_mcc_amt` · `f_hr_mcc_share` ·
`f_cb_refund_amt` · `f_returned_pay_amt`.

**Loan (6)**: `f_payments_vs_scheduled` · `f_extra_vs_loan` · `f_n_third_party_pay` · `f_cash_pay_amt` · `f_months_since_open` ·
`f_disb_outflow_ratio` (outflow within 7 days of a disbursement ÷ disbursement).

## 3. Event creation

1. `anomaly_score = −score_samples(X)` (higher = more anomalous); `anomaly_pct` = percentile rank within the product's rows.
2. Threshold score = **99th percentile** of `anomaly_score` within the product (`ML_EVENT_PERCENTILE`). Rows with
   `anomaly_score ≥` threshold become events, i.e. **≈1% of rows per product by construction**, regardless of how risky the population is.
3. Points = `10 + 15 × min(1, (anomaly_pct − 99) ÷ 1)` → 10 at the cut-off, 25 at the maximum. No severity multiplier.
4. **Reason codes**: SHAP `TreeExplainer` on the flagged rows; sign flipped so positive = more anomalous; top 3 positive
   features, rendered `"<label>: <value> (typical <median across all rows>)"`, joined with `" | "`.
5. **Trigger transactions**: transactions of that account-month matching the filters of the top-3 features (union);
   if none match, the 5 largest; if more than 40, the 40 largest by amount.
6. `fire_ts` = last transaction of the account-month (so cycle = that month); `window_start` = first transaction of the month.
7. `observed` = "Anomaly score s − top p% of <product> account-months in <month>"; `threshold` = "Top 1% (score ≥ t)".
   `ratio_to_threshold = score ÷ threshold score`.

## 4. Outputs
`ml_model_catalog` (one row per model: features, `score_threshold`, `n_training_rows`, `n_events`), `ml_features`
(every scored row with score and percentile — reusable for supervised work), ML events/links merged with rule events.

## 5. Open questions

- **Q-20 Fixed alert volume.** Because the cut-off is a percentile of the same data, ~1% of account-months flag every run even if nothing unusual happened, and a genuine surge would not flag more.
- **Q-21 No train/score split.** The model sees the months it scores (including injected laundering), so scores are in-sample; scores for one month depend on the other five.
- **Q-22 Not reproducible across data changes.** Adding a month changes every threshold and percentile; events can appear/disappear for old months on re-run.
- **Q-23 Dormant accounts are invisible.** Accounts with no activity have no row; "silence" is not itself anomalous here.
- **Q-24 "Typical" is the population median**, not the party's or segment's history, though the label wording implies a norm.
- **Q-25 Event points depend only on percentile**, not on the amount at risk. A 99.1-percentile row on $200 scores like one on $200,000.
- **Q-26 Reason-code transactions are heuristic.** The linked transactions are those *matching* the explained features' filters, not necessarily those that moved the score.
- **Q-27 Model catalogue** lists `version "1.0"` and no training date, data window, or owner — gaps for SR 11-7 style documentation.
