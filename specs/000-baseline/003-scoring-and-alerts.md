# Baseline 003 — Scoring and alerts (AS-IS)

| | |
|---|---|
| Status | AS-IS (reverse-engineered; awaiting SME review) |
| Code | `scoring_alerts.py` — `effective_points`, `score_parties`, `build_alert_tables`; `show_alert.py` |

## 1. Score hierarchy
```
transaction contribution → event effective points → account score → party alert score
```

## 2. Event effective points (repeat discount)
Group events by `(account_id, cycle_month, source_id)`; within a group, order by points descending.
- Rank 0: 100% of points. Every other event: `points × 0.25` (`RULE_REPEAT_FACTOR`).
- Group total capped at `2.0 ×` the group's highest points (`RULE_REPEAT_CAP`); if exceeded, all effective
  points in the group are scaled down proportionally. Rounded to 0.01.
- Different rules (or different accounts) on the same party are **not** discounted against each other.

## 3. Party score (per primary party × cycle month)

```
score = rule_points + ml_points + cross_product_bonus + kyc_bonus      (rounded to 0.1)
```
- `rule_points` / `ml_points`: sums of effective points by `event_type`.
- `cross_product_bonus`: let *n* = number of distinct `product_type` values among the **TRIGGER transactions**
  of that party-month's events (deposit/card/loan, taken from the transaction, not the event). Bonus = 10 if n=2, 15 if n≥3, else 0.
- `kyc_bonus`: +5 if the primary party's `kyc_risk == HIGH`.
- Parties with no events that month score 0 (a row exists for every primary party × month: 5,000 × 6 = 30,000).
- `alerted = score ≥ threshold` (35 in the pipeline); `near_miss = not alerted and score ≥ threshold − 15`.

## 4. Alert tables (alerted party-months only)

- `alert_id`: `A-00001…`, assigned by sorting alerted rows by `(cycle_month asc, score desc)`. Not stable across reruns.
- `alerts`: header — score and its parts, products, scenario list, number of triggering accounts, linked secondary
  parties, party KYC profile, `status = "OPEN"`, `created_date` = first day of the month after the cycle.
- `alert_accounts`: every triggering account with `account_score`, `pct_of_alert_score`, scenarios, secondary
  holders; plus the party's other primary accounts as non-triggering context (`account_score = 0`).
- `alert_events`: events with observed/threshold/ratio/severity/points/effective points/model score/reason codes and typology.
- `alert_transactions`: event↔transaction links with `points_contribution` re-scaled to effective points,
  `txn_score_in_alert` (sum across events), counterparty name, `initiated_by_party_id`, plain-English `reason`.

## 5. Reconciliation (invariant)
Transaction contributions → event effective points → account scores → alert score (plus bonuses) agree within 0.05 points.
Currently verified manually/by the README, **not by an automated test**.

## 6. Open questions

- **Q-30 Cross-product bonus is partly automatic.** R-XP-01 and R-LN-02 link transactions from two products, so they earn the +10 bonus on their own; the bonus then rewards the rule's design, not extra evidence.
- **Q-31 Bonus uses trigger *transaction* products, not event products**, so an ML event whose trigger set includes a transaction of another product could earn a bonus.
- **Q-32 No suppression.** A party stays above threshold month after month → a new alert each month with no link to earlier ones; no hibernation after a closed false positive.
- **Q-33 Alerts only on the primary party.** In joint-holder schemes the bad actor is a secondary holder (visible only in `alert_transactions.initiated_by_party_id`).
- **Q-34 Month boundary.** Behaviour spread over two months scores in two cycles and may stay below 35 in each; there is no rolling-window party score.
- **Q-35 Repeat discount ignores time.** Two hits in the same month are discounted; the same behaviour in consecutive months is scored in full each time.
- **Q-36 Threshold of 35 is below a single strong event's points** (e.g. R-DEP-01 at 5+ deposits = 35 pts), so one rule can alert alone; a single ML event (10–25 pts) needs bonuses or other events to reach 35 (the baseline has 9 ML-only alerts at threshold 35).
- **Q-37 Dependent on rule point values** being comparable: base points (12–30) are expert judgement with no documented calibration.
- **Q-38 IDs/ordering.** `A-nnnnn` numbering changes if any score changes, which would break case references.
