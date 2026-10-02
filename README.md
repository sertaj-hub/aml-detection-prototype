# Retail AML Detection Prototype — Synthetic Data, Rules, ML & Party Alerts

End-to-end prototype of transaction monitoring for a retail bank across **deposits, credit cards and loans**:
6 months of synthetic data (Apr–Sep 2026) → 15 rules + 3 anomaly models → transaction-to-account-to-party
scoring → **party-level alerts with full drill-down** → evaluation against ground truth.

Everything is synthetic and reproducible (fixed seed 42). Nothing here is real customer data.

---

## 1. How to run

```bash
pip install -r requirements.txt
python3 run_pipeline.py            # build data + detect + alert + evaluate   (~3 min)
python3 run_pipeline.py --no-data  # rerun detection on existing data/        (~2 min)
python3 show_alert.py --top 3      # print the 3 highest-scoring alerts as investigator trees
python3 show_alert.py A-00005      # print one alert
```

| File | What it does | Java mental model |
|---|---|---|
| `config.py` | All parameters: population, product mix, high-risk lists, scoring weights, threshold | `final class AmlConfig` with constants |
| `common.py` | ID generators, columnar transaction buffer, date helpers | utility classes |
| `generate_data.py` | Parties, accounts, roles, counterparties, normal behaviour, save | data factory / builder |
| `typologies.py` | 18 laundering typologies + legitimate look-alikes (false-positive fodder) | strategy objects per typology |
| `rules_engine.py` | 15 rules → detection events + linked transactions | `interface Rule { List<Event> evaluate(ctx) }` |
| `ml_events.py` | Per-product IsolationForest + SHAP reason codes → ML events | model wrapper + feature builder |
| `scoring_alerts.py` | Repeat discount, roll-up, bonuses, alert tables | scoring service |
| `evaluate.py` | Precision / recall vs ground truth, threshold sweep | test harness |
| `show_alert.py` | Prints an alert as a drill-down tree | CLI view |

---

## 2. The data (what was generated)

| | Count |
|---|---|
| Parties | 5,600 (5,000 primary holders + 600 secondary-only, e.g. spouses) |
| Accounts | 10,746 — checking 4,175, savings 2,061, CD 371, credit card 2,747, personal 580, auto 532, mortgage 280 |
| Accounts per primary party | 2.15 average |
| Secondary roles | joint 1,213, authorised user 519, co-borrower 210, guarantor 67 |
| Transactions | 1,252,298 — deposit 730,993, card 512,702, loan 8,603 |
| Laundering schemes | 71 across 18 typologies, 88 suspicious parties (1.57%), 2,674 suspicious transactions |
| Legitimate unusual behaviour | 659 parties (inheritance, car sale, investment moves, diaspora support, cash businesses, marketplace sellers, etc.) |

**Typologies injected** (≈35% run in a deliberately *subtle* variant designed to sit under simple rule thresholds):
structuring · rapid movement · funnel · mule rings (3 rings, mules + collector) · high-risk-country wires ·
dormant reactivation · cash inconsistent with profile · card overpay-and-refund · cash-advance cycling ·
third-party card payments · gambling/crypto MCC · bust-out · early loan payoff · loan proceeds moved out ·
third-party loan repayments · cash-to-credit (cross-product) · **joint holder is the bad actor** ·
**thin spread across many accounts** (each account looks fine alone).

### Data dictionary (`data/*.parquet`)

**Core entities**
- `parties` — party_id, name, dob, segment, occupation, annual_income, kyc_risk, pep_flag, state, onboarding_date, expected_monthly_credits, expected_monthly_cash, is_cash_intensive, is_secondary_only
- `accounts` — account_id, product_type (DEPOSIT/CARD/LOAN), product_subtype, open_date, status, branch_state, credit_limit, loan_amount, loan_term_months, interest_rate, monthly_payment, last_activity_pre_window
- `party_account_role` — party_id, account_id, role (PRIMARY / JOINT / AUTHORIZED_USER / CO_BORROWER / GUARANTOR), start_date, end_date. Exactly one PRIMARY per account.
- `counterparties` — counterparty_id, cp_name, cp_type (EMPLOYER, MERCHANT, INDIVIDUAL, SELF_EXTERNAL, FOREIGN_ENTITY, CRYPTO_EXCHANGE…), country, state, mcc, linked_party_id (set when the counterparty is a known party's account at another bank)
- `transactions` — txn_id, account_id, product_type, txn_ts, txn_type, direction, amount, channel, is_cash, counterparty_id, counter_account_id (other leg of an internal transfer), cp_country, mcc, location_state, initiated_by_party_id (who actually did it — matters for joint/AU), description
  - Direction convention: deposit CR = in; card DR = increases balance owed; loan DR = disbursement, CR = repayment
  - Product views with the same columns: `deposit_txns`, `card_txns`, `loan_ledger`

**Detection**
- `rule_catalog` — rule_id, rule_name, product_scope, typology, logic (plain English), base_points, version
- `ml_model_catalog` — model_id, model_type, product_scope, features, event rule, score_threshold, n_events
- `ml_features` — every account-month's features, anomaly_score and anomaly_pct (reusable for a supervised model)
- `detection_events` — event_id (RE… rule, ME… ML), event_type, source_id, account_id, primary_party_id, product_type, fire_ts, cycle_month, observed, threshold, ratio_to_threshold, severity, points, effective_points, model_score, reason_codes
- `event_transactions` — event_id, txn_id, link_role (TRIGGER / CONTEXT), points_contribution — the many-to-many link

**Scoring & alerts**
- `party_scores` — every primary party × month (30,000 rows): rule_points, ml_points, cross_product_bonus, kyc_bonus, score, threshold, alerted, near_miss
- `alerts` — header: alert_id, party_id, cycle_month, alert_score vs threshold, points breakdown, scenarios_hit, products_involved, linked_secondary_parties, and the party's KYC profile
- `alert_accounts` — every account of the alerted party: account_score, pct_of_alert_score, scenarios_hit, is_triggering, linked_secondary_parties
- `alert_events` — which rules / ML events fired, on which account, observed vs threshold, reason codes, effective points
- `alert_transactions` — the transactions behind each event, with points_contribution, txn_score_in_alert, counterparty, initiated_by, and the plain-English reason

**Ground truth (evaluation only — never read by detection)**
- `labels_transactions`, `labels_accounts`, `labels_parties` (incl. `innocent_primary_on_abused_account`), `schemes`, `legit_spikes`

---

## 3. Detection design

**Rules (15)** — see `rule_catalog`. Deposit 6, card 5, loan 3, cross-product 1 (`R-XP-01`, evaluated at party level across all products). Points = base points × severity (1.0 at threshold, up to 1.5 at 3× threshold).

**ML (3 models)** — IsolationForest per product on account-month features that compare behaviour to the party's own KYC profile (e.g. cash vs expected cash, credits vs declared income). Unsupervised: labels are not used. Account-months above the 99th percentile raise an ML event worth 10–25 points. SHAP gives the top-3 reason codes, and the transactions behind those features are linked to the event.

**Scoring** (`scoring_alerts.py`), monthly cycle, alerts on the **primary party**:
```
txn contribution → event effective points → account score → alert score
event effective points = points × repeat discount (same scenario repeating on the same account: 100% / 25% each, capped at 2×)
alert score = Σ account scores + cross-product bonus (+10 for 2 products, +15 for 3) + KYC high-risk (+5)
alert when alert score ≥ 35
```
Verified: every level reconciles to the one above within 0.05 points.

---

## 4. Results

**Threshold sweep** (`evaluation_threshold_sweep`). *Productive* = alert contains at least one account with real suspicious activity; *scheme recall* = share of the 71 schemes caught.

| Variant | Threshold | Alerts (6 mo) | Productive | Party recall | Scheme recall |
|---|---|---|---|---|---|
| Rules only | 20 | 507 | 30% | 82% | 89% |
| Rules only | 30 | 341 | 34% | 62% | 65% |
| Rules only | 40 | 101 | 77% | 42% | 54% |
| **Rules + ML** | **35** | **321** | **42%** | **78%** | **87%** |
| Rules + ML | 50 | 152 | 61% | 47% | 59% |

**At a matched alert volume** (~330 alerts, ~55/month), adding ML gives **6% fewer alerts, +8 pts productive rate, +22 pts scheme recall** (46 → 62 of 71 schemes).

Per typology, ML adds the most where rules are blind or thresholds are easy to stay under: cash inconsistent with profile (0 → 3 of 5), dormant reactivation (0 → 4 of 4), third-party card payments (0 → 2 of 3), high-risk wires (3 → 5 of 5). 7 of the 9 missed schemes are the deliberately subtle variants.

Individual rule precision (account-level) ranges from 3% (`R-LN-03`, swamped by legitimate cash loan payers) to 100% (`R-XP-01`); ML events run at 16–27%. The 298 near-miss party-months (scores 20–35) are kept in `party_scores` for below-the-line testing.

---

## 5. Caveats (read before quoting numbers)

- **Synthetic data is cleaner than reality.** Real SAR conversion rates are far lower; treat the absolute precision as illustrative and the *relative* rules-vs-ML comparison as the useful signal.
- **The threshold was chosen on the same data it is evaluated on**, so the numbers are optimistic. In production, tune on one period and validate on a later one, with below-the-line sampling.
- **IsolationForest is fit on all six months** (it's unsupervised, but there is no train/test split in time). A production version should train on history and score forward.
- **Alerts go to the primary party by design.** In the 3 joint-holder schemes the alert correctly lands on the abused account, but the bad actor is the linked secondary — visible in `alert_transactions.initiated_by_party_id`. That's a design point worth debating with your FIU.
- Lists like high-risk countries are illustrative, not official.

---

## 6. Natural next steps

1. **Supervised model** on `ml_features` + labels (gradient boosting, time-based split) to rank alerts rather than just raise them.
2. **Alert review page** — click from party → account → event → transaction.
3. **Graph features** (shared counterparties, mule-ring structure) — the mule rings are an obvious target.
4. Model documentation in the SR 11-7 style: purpose, data, assumptions, limitations, monitoring.

---

`exports/` holds CSV copies of the alert tables, catalogs, threshold sweep and scheme list (open in Excel), plus `sample_alert_trees.txt` with five printed alerts, including a joint-holder case (A-00005) and a cross-product case (A-00060).
