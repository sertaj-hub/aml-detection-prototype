# Baseline 005 — Open questions for the AML SME

Each question was raised while reverse-engineering the code. For each, mark one: **KEEP** (document as intended),
**CHANGE** (becomes a new spec), or **DEFER**.

| ID | Where | Question | Why it matters | Decision |
|---|---|---|---|---|
| Q-01 | rules | Rule thresholds are code literals, no per-segment values | Tuning needs a code change; cash-intensive and student segments share thresholds | |
| Q-02 | R-CRD-02 | Refund fires without any prior overpayment | Catalog text and logic disagree; likely false positives | |
| Q-03 | R-CRD-05 | Any single returned payment = 25 pts | Common event; no corroboration needed | |
| Q-04 | R-DEP-02 | "Out" counts all debits ≥500 incl. POS/internal | Normal spending can satisfy the 80% outflow test | |
| Q-05 | R-DEP-04 | Any CR counts; first wake only | Misses second reactivations; counts internal transfers | |
| Q-06 | R-XP-01 | Cash card payments count as cash-in and as paydown | Rule can fire on cash card payments alone | |
| Q-07 | R-LN-01 | $10k floor for all loan types | Mortgage/auto lump sums are routine | |
| Q-08 | R-LN-02 | Legit loan proceeds paid out (dealer/seller) | Auto/mortgage false positives | |
| Q-09 | rules | Calendar-month windows split month-end behaviour | Evasion by timing; missed alerts | |
| Q-10 | R-DEP-06 | Inbound limited to individuals/internal/out-of-state cash | Wire or business funnels missed | |
| Q-11 | rules | `initiated_by_party_id` unused by rules | Joint/AU abuse invisible to rules | |
| Q-12 | windows | Inclusive 7/3/14/21-day boundaries | Needs explicit, tested semantics | |
| Q-13 | severity | Ratios not comparable across rules | Points inconsistent | |
| Q-14 | R-DEP-01 | Band is 8,000–9,999.99 | Structuring below 8,000 invisible | |
| Q-20 | ML | ~1% of rows flag by construction | Volume independent of risk | |
| Q-21 | ML | No train/score split | Optimistic, unstable scores | |
| Q-22 | ML | Percentiles shift when data is added | Events not reproducible across runs | |
| Q-23 | ML | Inactive account-months not scored | Silent accounts invisible | |
| Q-24 | ML | "Typical" = population median | Misleading to investigators | |
| Q-25 | ML | Points ignore amount at risk | Small-value anomalies rank high | |
| Q-26 | ML | Linked txns match filters, not SHAP impact | Explanation may mislead | |
| Q-27 | ML | No model training metadata | Governance gap | |
| Q-30 | scoring | XP bonus partly automatic | Double reward | |
| Q-31 | scoring | Bonus from transaction products | Unintended bonus for ML events | |
| Q-32 | scoring | No suppression / hibernation | Repeat alerts on the same party | |
| Q-33 | scoring | Alerts only on primary party | Wrong person named in joint schemes | |
| Q-34 | scoring | Month-end splits party score | Missed alerts | |
| Q-35 | scoring | Repeat discount ignores time | Persistent behaviour scores in full each month | |
| Q-36 | scoring | One rule can alert alone | Alert volume sensitive to one rule's points | |
| Q-37 | scoring | Base points uncalibrated | Weights are judgement only | |
| Q-38 | scoring | Alert IDs not stable | Breaks case references | |
| Q-40 | eval | Everything in-sample | Optimistic | |
| Q-41 | eval | No per-rule metrics in code | README figures not reproducible by a command | |
| Q-42 | eval | Productive ≠ SAR | Do not quote absolute precision | |
| Q-43 | eval | Scheme recall is "any account" | Coarse | |
| Q-44 | eval | Party precision structurally low | Misleading metric | |
| Q-45 | eval | No workload metric | Thresholds judged on precision only | |

Suggested first priorities (my view, for discussion): Q-02, Q-03, Q-06 (clear logic/catalog mismatches),
Q-32 and Q-38 (production blockers), Q-21 (credibility of ML numbers).
