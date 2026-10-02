# Baseline 000 — System overview (AS-IS)

| | |
|---|---|
| Status | AS-IS (reverse-engineered from code at commit `e8ed4eb`; awaiting SME review) |
| Covers | `run_pipeline.py`, `config.py`, `common.py`, data flow between stages |
| Detail specs | `001-detection-rules.md`, `002-ml-anomaly-events.md`, `003-scoring-and-alerts.md`, `004-evaluation.md` |

This describes what the code **does today**, including behaviour we may later want to change. Disagreements
become new specs (`010+`), not edits here. Items marked **Q-nn** are open questions for the AML SME; they are
collected in `005-open-questions.md`.

## 1. Purpose
Monthly transaction monitoring for a retail bank across deposits, credit cards and loans. Produces one alert
per primary party per monthly cycle when the party's accumulated detection points reach a threshold.

## 2. Stages

| # | Stage | Code | Input | Output |
|---|---|---|---|---|
| 1 | Generate synthetic data (optional) | `generate_data.py`, `typologies.py` | `config.py` | `data/*.parquet` incl. ground-truth `labels_*`, `schemes`, `legit_spikes` |
| 2 | Rules | `RulesEngine.run()` | `transactions`, `accounts`, `parties`, `party_account_role`, `counterparties` | rule events + event↔transaction links |
| 3 | ML | `MLEventBuilder.run()` | same, via the rules engine's loaded data | ML events + links + `ml_features`, `ml_model_catalog` |
| 4 | Threshold sweep | `evaluate.compare()` | events, links, labels | `evaluation_threshold_sweep` |
| 5 | Scoring | `score_parties()` | events, links | `party_scores` (every primary party × month) |
| 6 | Alert tables | `build_alert_tables()` | scored events + reference tables | `alerts`, `alert_accounts`, `alert_events`, `alert_transactions` |
| 7 | Chosen-threshold evaluation | `evaluate()` | scores, labels | printed metrics |

`run_pipeline.py --no-data` skips stage 1 and reuses `data/`.

## 3. Key definitions

- **Cycle**: calendar month (`2026-04` … `2026-09`). An event's cycle is the month of its `fire_ts`.
- **Primary party**: the single PRIMARY-role party on an account. All events and alerts attach to it; JOINT,
  AUTHORIZED_USER, CO_BORROWER and GUARANTOR parties are context only.
- **Event**: one rule hit or ML flag on one account, with observed value, threshold, points and linked transactions.
- **Third party**: counterparty present on a transaction whose linked party is not a member (any role) of the account.
- **Product types**: DEPOSIT, CARD, LOAN; events from R-XP-01 carry product `CROSS_PRODUCT`.

## 4. Configuration (`config.py`)

| Parameter | Value | Used by |
|---|---|---|
| `SEED` | 42 | data generation, IsolationForest |
| `ALERT_THRESHOLD` | 35 | alerting |
| `NEAR_MISS_BAND` | 15 | `near_miss` flag (score in [20, 35)) |
| `RULE_REPEAT_FACTOR` / `RULE_REPEAT_CAP` | 0.25 / 2.0 | repeat discount |
| `CROSS_PRODUCT_BONUS` | {2: 10, 3: 15} | party score |
| `KYC_HIGH_RISK_BONUS` | 5 | party score |
| `ML_EVENT_PERCENTILE` | 99.0 | ML event cut-off |
| `ML_POINTS_MIN/MAX` | 10 / 25 | ML event points |
| `HIGH_RISK_COUNTRIES` | IR KP MM SY YE HT VE SS (illustrative) | R-DEP-03, ML features |
| `HIGH_RISK_MCC` | 7995, 6051 | R-CRD-04, ML features |

Rule thresholds (amounts, windows, counts) are **literals inside `rules_engine.py`**, not in `config.py` (Q-01).

## 5. Invariants observed in the code
1. Labels are read only in `run_pipeline.py` (loaded, passed to `evaluate`/`compare`) and `evaluate.py`; rules, ML and scoring never receive them.
2. Every event links ≥1 TRIGGER transaction; contributions are pro-rated by amount and sum to event points (rounded to 0.01).
3. Alert-level tables re-scale contributions so they sum to the event's **effective** points.

## 6. Baseline results (Rules + ML)
Threshold 35: 321 alerts, 190 alerted parties, productive 42.1%, party precision 34.7%, party recall 77.6%,
scheme recall 87.3% (62/71). Rules only at 35: 131 alerts, productive 67.2%, scheme recall 56.3%.
Source: `exports/evaluation_threshold_sweep.csv`. Caveat: threshold tuned and evaluated on the same data.

## 7. Known structural limits (not defects of a single rule)
- Whole-window batch run; no incremental cycles; IDs (`RE…`, `ME…`, `A-nnnnn`) are run-order counters, not stable.
- Alert status is always `OPEN`; no suppression of parties already alerted in earlier months.
- Output files are overwritten on each run; no run/version metadata.
