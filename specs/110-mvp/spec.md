# Spec 110 — Rule Engine MVP (first release)

| | |
|---|---|
| Status | DRAFT v2 — scope and MQ-2..5 approved by the user ("Yes"); rules revised to the U.S. TM set, awaiting confirmation of rule parameters (RQ-1..RQ-8) |
| Author | Claude (with the user, AML SME) |
| Approver | |
| Created | 2026-10-02 |
| Related | `specs/100-platform/01` (PR-n, D-1..D-9), `02` (FR-n, AC-n), `specs/000-baseline/` |

## 1. Context and problem
The platform specs (`100-platform`) describe the full product. The MVP proves the **core detection path end to
end** on a small slice, so the main design risks (rule definition language, set-based evaluation, detections,
novelty, party-level alerts, atomic persistence, outbox) are tested with working code before the rest is built.

MVP means: *a rule defined as JSON runs in a daily batch over transaction data and produces explainable,
deduplicated, party-level alerts that are stored reliably and ready to publish.*

## 2. Scope

**In scope (MVP)**
- Python package `ruleengine/` (in this repository for now; it can move to its own repository later).
- **Daily** batch run for one business date (and a loop over a date range).
- Rule definitions as **JSON files** (rule definition language v0), validated against a JSON Schema and a field catalogue.
- Rule definition language v0 covers **T1** (filters), **T2** (count/sum/max in a rolling window, grouped by account or party), **T3** (comparison with a party-profile attribute) and a **T4-lite** (ratio of two aggregations over the same window).
- **Compile rules to SQL** and execute with **DuckDB** over the prototype's Parquet transactions (stand-in for Databricks; D-2).
- Detections with deterministic ids and reconciling evidence; the **novelty rule** (FR-37).
- **Party-level alerts** (one per primary party per business date, D-4/D-6) with points and a threshold from a policy file.
- Persistence of runs, detections, alerts, evidence and an **outbox** in one transaction per alert (FR-46), on **SQLite** with plain portable SQL (PostgreSQL later).
- Outbox **publisher stub** (writes the event JSON to a file/log; Kafka is not in the MVP) with retry state.
- Idempotent re-run of a business date.
- A command line: `python -m ruleengine run --date 2026-06-15` / `--from --to`, and `validate <rule.json>`.
- Run record with a manifest (rule versions, policy version, data snapshot hash).

**Out of scope (later releases)**
- UI, REST API, approval workflow and roles (MVP rules are files under version control; `status` is read from the file).
- Databricks, Kafka, PostgreSQL, Spring Boot control plane.
- MONTHLY cycle, Security Blanket, calendar-month rules.
- Reprocessing with withdrawal (MVP re-run is idempotent only), late-data handling, completeness gate.
- Enrichment beyond account → primary party and joint holders; reference-data versioning.
- Tests/backtests as separate modes (the CLI can simply run without persisting; see FR-M12).
- Sequence/pattern rules beyond T4-lite, behaviour against own history, relationship rules (T5–T6); alert policy governance; threshold sets per segment.
- Rules **TM-US-004** and **TM-US-005** (PATRIOT Act §312) are specified but **deferred**: the synthetic data has no correspondent-banking or private-banking attributes (see §3.2).

## 3. MVP rules (U.S. BSA / PATRIOT Act anchored)

Rules follow the U.S. transaction-monitoring set proposed by the user. Regulatory references are **context for
compliance review, not legal advice**; the mapping of each rule to a regulation is to be validated by Compliance.
Each rule file carries `regulation: { framework, references[], note }` metadata (free text, shown in evidence).
Parameters (thresholds, windows, floors) are **named parameters in the rule file**, never constants in the engine.

### 3.1 In the MVP

**TM-US-001 Potential Structuring** (T2, evaluated at each qualifying transaction time) — BSA: structuring around the $10,000 currency-transaction reporting threshold.
- Scope: cash transactions only (`is_cash`). Cash-in = `CASH_DEPOSIT`; cash-out = `ATM_WITHDRAWAL` (the data has no `CASH_WITHDRAWAL` type; the mapping is a parameter).
- Per-transaction condition: `amount < ctrThreshold` (parameter, 10,000).
- Grouping: **primary party** (across the party's accounts where the party is PRIMARY) **and direction** (cash-in and cash-out aggregated separately; see RQ-2).
- Window: trailing **24 hours** ending at each qualifying transaction.
- Thresholds: `count ≥ 3` and `sum ≥ ctrThreshold`.
- Severity: HIGH. Severity ratio = max(count ÷ 3, sum ÷ ctrThreshold).
- Evidence: the qualifying transactions, their times and amounts, count, sum, window, threshold, and the sentence "3 cash deposits between $2,900 and $4,800 totalling $11,400 within 24 hours, each below the $10,000 reporting threshold".
- Note: $10,000 is the currency-reporting reference point, not a detection rule on its own (FinCEN: monitoring must be risk-based). Windows of several days are a common variant; the 7-day parameter set is run as a sensitivity in the backtest (RQ-3).

**TM-US-002 Unusual High-Value Activity** (T3, evaluated at end of each business date) — BSA: suspicious-activity monitoring against the customer profile.
- Scope: external credits (`direction = CR`, `channel != INTERNAL`).
- Grouping: primary party. Profile attribute: `party.expected_monthly_credits` (floored at `profileFloor`, 500).
- Condition (either): **(a)** a single credit ≥ `singleMultiple` × expected monthly credits and ≥ `absoluteFloor`; **(b)** the trailing 30-day credit sum ≥ `aggregateMultiple` × expected monthly credits and ≥ `absoluteFloor`.
- Proposed parameters: `singleMultiple` 1.5, `aggregateMultiple` 3.0, `absoluteFloor` 10,000 (to be confirmed, RQ-4).
- Severity: MEDIUM. Severity ratio = observed ÷ (multiple × expected).
- The novelty rule stops a persistent aggregate from re-alerting daily.

**TM-US-003 Rapid Movement of Funds** (T4-lite, evaluated at each outgoing transaction time) — BSA: layering / pass-through.
- Grouping: account. Window: trailing **48 hours** ending at each outgoing transaction.
- Credits: external credits (`CR`, `channel != INTERNAL`): `count ≥ 3` and `sum ≥ creditFloor` (10,000).
- Outflows to **unrelated parties**: `WIRE_OUT`, `P2P_OUT`, `CHECK_PAID`, `ATM_WITHDRAWAL`, excluding transfers to the customer's own accounts (`SELF_EXTERNAL`, `TRANSFER_OUT`).
- Condition: outflow sum in the window ≥ `outflowRatio` (0.8) × credit sum.
- Severity: MEDIUM. Severity ratio = (outflow ÷ credit) ÷ outflowRatio.

### 3.2 Specified but deferred: PATRIOT Act §312 rules
USA PATRIOT Act §312 (31 U.S.C. 5318(i)) requires due diligence, and enhanced due diligence for certain accounts, on foreign correspondent and private banking accounts. Monitoring rules are the way the bank evidences ongoing scrutiny; they are not themselves mandated rule text.

| Rule | Needs | Available in the synthetic data? |
|---|---|---|
| TM-US-004 High-Risk Correspondent Activity | correspondent relationship flag, foreign financial institution attributes, expected purpose and volumes | No (only `FOREIGN_ENTITY` counterparties and high-risk-country wires) |
| TM-US-005 Private Banking / Foreign Political Figure Activity | private-banking relationship flag, source of funds, senior foreign political figure flag, expected use | Partly: `pep_flag` (11 parties, all AFFLUENT); no private-banking or source-of-funds data |

A separate spec (`120-synthetic-data-extension`) will add these attributes and typologies to the data generator, after which the two rules join the engine. They are listed here so the rule definition language and field catalogue are designed to accommodate them (profile attributes and relationship flags).

### 3.3 MVP scoring policy (MVP only)
Points by rule severity: HIGH 40, MEDIUM 30, LOW 15, multiplied by the severity factor `1 + 0.25 × (ratio − 1)` capped at 1.5. Party-level alert threshold 35 (so one HIGH detection alerts on its own; MEDIUM detections need a ratio ≥ 1.2 or a second detection). Production thresholds remain OQ-F13.

### 3.4 Language additions needed (v0)
`regulation` metadata; `scope.transactionTypes` and `scope.cashOnly`; per-transaction `conditions`; `groupBy` ∈ {ACCOUNT, PARTY} plus optional `splitBy: direction`; named `aggregations` (function, filter, window); `profile` references (`party.expected_monthly_credits`); `conditions` over named aggregations including ratios; `evaluate` ∈ {EVENT_TIME, SNAPSHOT}; named `parameters`.

## 4. Functional requirements (MVP subset of `02`)

- **FR-M1** (FR-12, FR-11) A rule file is validated: JSON Schema, only catalogue fields, operators valid for the field type, unique `ruleId`+`version`. Errors carry the JSON path.
- **FR-M2** (FR-10) The language v0 supports: `filters` (field, operator, value or list), `aggregation` (`COUNT`, `SUM`, `MAX` over the filtered transactions), `window` (`ROLLING`, N days, evaluated per FR-29), `groupBy` (`ACCOUNT`), `threshold` (operator, value), `severity` ratio definition. No user code is executed.
- **FR-M3** (FR-33, 35) Evaluation is generated as SQL (no row loops). The same SQL runs on DuckDB in tests and is portable to Spark SQL.
- **FR-M4** (FR-28, 29, 31, 32) Window boundaries, rounding and null handling follow `02` exactly. `EVENT_TIME` rules evaluate a trailing window `(t − N, t]` at each candidate transaction time `t` within the business date; `SNAPSHOT` rules evaluate `(end(D) − N, end(D)]`. Amounts are compared at 2 decimals half-up; null never matches.
- **FR-M5** (FR-36, 39) A match creates a detection with `detectionId = hash(ruleId, entityKey, sorted trigger txn ids, windowEnd)`, rule version, window, run id, metrics and trigger transactions.
- **FR-M6** (FR-37, 38) Novelty rule: a detection with no new trigger transaction compared with earlier detections of the same rule and entity is stored as `SUPPRESSED_NO_NEW_EVIDENCE` and forms no alert.
- **FR-M7** (FR-40) Evidence holds metrics, threshold, window, rule version, trigger transactions and a plain-English sentence; all numbers must reconcile with the linked transactions or the alert is not created and the failure is recorded.
- **FR-M8** (FR-41, D-4/5/6) For each business date, unconsumed new detections are grouped by the **primary party** of the account; the party's score = sum of detection points; if score ≥ threshold, one alert is created listing every detection and the joint/secondary holders with their roles.
- **FR-M9** (FR-43, 46, 47) Alert, evidence, links and outbox row are written in one transaction. `alertKey = hash(primaryPartyId, "DAILY", businessDate, policyVersion)`.
- **FR-M10** (FR-21, 22) A run record stores status, counts and a manifest (rule files and their hashes, policy version, input data hash). A rule that fails is recorded and the others continue; run status `PARTIAL` (FR-23).
- **FR-M11** (FR-26 reduced) Re-running a business date with unchanged inputs creates no new detections, alerts or outbox rows.
- **FR-M12** (FR-52 reduced) `--dry-run` computes and prints detections and would-be alerts and writes nothing.
- **FR-M13** (FR-48, 49 reduced) The publisher stub claims PENDING outbox rows, writes the event JSON (alertId, alertKey, ruleIds and versions, primaryPartyId, severity, businessDate, eventId) to `out/alerts.jsonl`, marks PUBLISHED, and increments attempts/`FAILED` after `maxAttempts` on error.
- **FR-M14** (Constitution 3) The engine never reads label tables; a test asserts that detection code has no access path to `labels_*`.

## 5. Acceptance criteria (each becomes a named test)

Rule definition and validation
- **MAC-1** (FR-M1) Given a rule referencing field `transaction.foo`, Then validation fails with the JSON path and no run is attempted.
- **MAC-2** (FR-M1) Given two files with the same `ruleId` and `version` but different content, Then loading fails.

Evaluation (using small hand-built transaction fixtures)
- **MAC-3** (TM-US-001) Three cash deposits of $3,000, $3,500 and $4,500 within 24 hours (sum $11,000) match; two deposits do not; three deposits totalling $9,999.99 do not; a deposit of exactly $10,000.00 does not qualify (not below the threshold).
- **MAC-4** (TM-US-001 window) Deposits at t, t+12h and t+24h: the first is excluded from the window ending at t+24h (start exclusive), so no match; at t, t+12h and t+23h59 the three match.
- **MAC-5** (TM-US-001 grouping) Deposits spread over two accounts of the same primary party count together; the same amounts across two different parties do not; cash-in and cash-out are not combined (RQ-2).
- **MAC-6** (TM-US-001 scope) Non-cash deposits (`is_cash` false) and non-cash transaction types never qualify.
- **MAC-7** (TM-US-002) Expected monthly credits 4,000: a single credit of 12,000 (≥ 1.5× and ≥ floor) matches; 5,999 does not; a trailing-30-day sum of 12,000 (3×) matches; 11,999 does not; with expected credits 500 (floored) a credit of 9,000 does not match because it is below the absolute floor.
- **MAC-8** (TM-US-003) Three external credits totalling 15,000 in 48h and outflows of 12,000 to unrelated parties match (80%); outflows of 11,900 do not; outflows to the customer's own external account do not count; two credits do not match.
- **MAC-9** (FR-32) A null amount, null cash flag or null expected-credits value never matches.

Detections, novelty, alerts
- **MAC-10** (FR-M5) The same inputs produce identical `detectionId`s; changing one trigger transaction changes the id.
- **MAC-11** (FR-M6) TM-US-001 matched on days D and D+1 with the same three deposits: the second detection is suppressed and no alert is created; adding a fourth deposit on D+2 creates a new detection.
- **MAC-12** (FR-M8) A party with two accounts and detections from two rules scoring ≥ 35 gets exactly one alert with both detections; a party scoring 34.9 gets none (policy points are set in the test fixture).
- **MAC-13** (FR-M8) A detection on a joint account is addressed to the primary party; the joint holder is listed with role JOINT; no alert for the joint holder.
- **MAC-14** (FR-M7) Evidence totals equal the sum of the linked transactions; if forced inconsistent, no alert is created and the failure is recorded.

Persistence, runs, outbox
- **MAC-15** (FR-M9) A failure injected between the alert insert and commit leaves no alert, evidence, links or outbox row.
- **MAC-16** (FR-M11) Running the same date twice creates no additional detections, alerts or outbox rows.
- **MAC-17** (FR-M10) A rule with an execution error is recorded; other rules' results exist; run status `PARTIAL`.
- **MAC-18** (FR-M12) `--dry-run` writes no rows.
- **MAC-19** (FR-M13) The stub publishes pending rows once, in order, marks them PUBLISHED, and a simulated failure increments attempts and ends in FAILED after the limit.
- **MAC-20** (FR-M14) No module under `ruleengine/` reads `labels_*` files.

Conformance with the prototype (informational, not pass/fail; differences explained)
- **MAC-21** Run `TM-US-001` over Apr–Sep 2026 on the prototype data and compare with the prototype's `R-DEP-01` events: report the number of accounts detected by both, only by the prototype, only by the engine. Expected differences (greedy episodes vs. daily rolling evaluation plus the novelty rule) are listed in section 7. Report precision/recall against the labels in the **evaluation script only** (never the engine).
- **MAC-22** Run `TM-US-001/002/003` over the six months and report: detections, suppressed, alerts at threshold 35, and the run time. Target: one daily run under 60 seconds on the 1M-row-per-day scale is **not** an MVP criterion; the six months of prototype data (1.25M rows) must complete in under 5 minutes in total.

## 6. Data contracts (MVP)
Input (existing prototype Parquet): `transactions`, `accounts`, `party_account_role`, `counterparties`.
Field catalogue v0: `transaction.amount, txn_type, direction, channel, is_cash, cp_country, counterparty_type, account_id, txn_ts, txn_id`; `account.product_type`; `party.expected_monthly_credits, expected_monthly_cash, pep_flag, kyc_risk`; `party.id` via the PRIMARY role.
Output tables (SQLite): `run`, `rule_execution`, `detection`, `detection_transaction`, `alert`, `alert_detection`, `alert_party`, `alert_evidence`, `outbox_event`. Full DDL in the implementation and later in `03-domain-model`.

## 7. Known differences from the prototype (to be reported, not hidden)
- Prototype `R-DEP-01` finds non-overlapping episodes over the whole period; the engine evaluates each business date over a rolling window and applies the novelty rule, so the number and timing of detections will differ.
- Prototype scoring includes ML points, repeat discounts and cross-product/KYC bonuses; the MVP policy is a plain sum.
- Enrichment, ML anomaly events and the other 12 rules are not in the MVP, so alert counts are not comparable to the baseline of 321.

## 8. AML and regulatory considerations
Typologies: cash structuring, high-value cash, high-risk geography wires. Every alert is explainable and
reproducible (manifest). The MVP is a technical proof, not a production monitoring system.

## 9. Model risk impact
None on the prototype. The MVP is a separate package that reads the prototype data. Conformance differences are
documented (section 7, MAC-19).

## 10. Constitution check
Explainability (FR-M7) · reproducibility (manifest, deterministic ids, MAC-8/14) · labels out of detection (FR-M14) ·
no silent change (conformance report) · honest evaluation (evaluation script separate) · synthetic data only ·
simplicity (SQLite, stub publisher, three rules).

## 11. Open questions
Approved by the user ("Yes"): **MQ-1** MVP scope, **MQ-2** SQLite, **MQ-3** `ruleengine/` in this repository, **MQ-4** threshold 35 and plain-sum scoring (MVP only), **MQ-5** dependencies `duckdb`, `jsonschema`, `pytest`.

Rule parameters to confirm with the SME before coding:
- [ ] **RQ-1** `TM-US-001` shall use cash-in `CASH_DEPOSIT` and cash-out `ATM_WITHDRAWAL` (no `CASH_WITHDRAWAL` exists in the data). OK?
- [ ] **RQ-2** Aggregate cash-in and cash-out **separately** (my understanding is that currency-transaction aggregation treats them separately), or combined as in the proposed YAML? Please confirm with Compliance.
- [ ] **RQ-3** 24-hour window as proposed. Keep also a 7-day variant as a backtest sensitivity (not an MVP rule)?
- [ ] **RQ-4** `TM-US-002` parameters: single credit ≥ 1.5× expected monthly credits, trailing-30-day sum ≥ 3×, absolute floor $10,000. Adjust?
- [ ] **RQ-5** `TM-US-003`: ≥ 3 credits, ≥ $10,000, 48-hour window, outflow ≥ 80% to unrelated parties. Adjust? Are `ATM_WITHDRAWAL` and `CHECK_PAID` "outgoing transfers" for this rule?
- [ ] **RQ-6** Grouping by primary party only (joint holders' activity on a shared account counts toward the primary): acceptable for the MVP?
- [ ] **RQ-7** MVP severity points HIGH 40 / MEDIUM 30 and threshold 35: acceptable for the MVP?
- [ ] **RQ-8** Defer TM-US-004/005 until a synthetic-data extension spec (`120`) adds correspondent and private-banking attributes?

## 12. Implementation notes
Not applicable until `IMPLEMENTED`.
