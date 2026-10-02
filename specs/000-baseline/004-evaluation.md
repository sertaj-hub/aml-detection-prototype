# Baseline 004 — Evaluation (AS-IS)

| | |
|---|---|
| Status | AS-IS (reverse-engineered; awaiting SME review) |
| Code | `evaluate.py` — `evaluate`, `compare`; invoked from `run_pipeline.py` |

## 1. Ground truth
`labels_accounts` (account-level `is_suspicious`, `scheme_ids`), `labels_parties` (`is_suspicious`), `schemes` (71 rows).
Used only here (and loaded by `run_pipeline.py` to hand to these functions).

## 2. Metrics (per variant × threshold)

| Metric | Definition (as coded) |
|---|---|
| `alerts` | alerted party-months |
| `alerted_parties` | distinct alerted parties |
| **Productive alert rate** | share of alerts where ≥1 *triggering account* (an account with an event in that party-month) is labelled suspicious |
| `party_precision` | alerted parties that are labelled suspicious ÷ alerted parties |
| `party_recall` | alerted parties that are labelled suspicious ÷ suspicious parties that exist among primary parties |
| `scheme_recall` | distinct schemes with ≥1 account in a productive alert ÷ all 71 schemes |

Productive credits an alert even when a joint holder, not the primary, is the bad actor.

## 3. Threshold sweep
`compare()` re-scores with three event sets — Rules only, ML only, Rules + ML — at thresholds
20, 25, 30, 35, 40, 45, 50, 60 and writes `evaluation_threshold_sweep`.

## 4. Baseline values (Rules + ML)
| Threshold | Alerts | Productive | Party recall | Scheme recall |
|---|---|---|---|---|
| 35 (chosen) | 321 | 42.1% | 77.6% | 87.3% |

Full table: `exports/evaluation_threshold_sweep.csv`.

## 5. Open questions
- **Q-40 In-sample.** The threshold was picked from this sweep on the same data; ML is fitted on it too. All numbers are optimistic.
- **Q-41 No per-rule or per-typology metrics in code** (the README quotes them; they were computed ad hoc).
- **Q-42 Productive ≠ SAR-worthy.** Real conversion rates are far lower; absolute precision should not be quoted.
- **Q-43 Scheme recall counts a scheme once any account is caught**, not how much of it, or how early.
- **Q-44 Party precision is low by design** because alerts land on the primary party while many bad actors are secondary or the abused account's owner is innocent (`innocent_primary_on_abused_account`).
- **Q-45 Sweep tests only threshold**, not alerts per investigator-month or workload.
