# Spec 110 — Rule Engine MVP (first release)

| | |
|---|---|
| Status | DRAFT |
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
- Rule definition language v0 covers **T1** (filters on a transaction) and **T2** (count/sum/max within a rolling window, grouped by account).
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
- T3–T6 rule types, alert policy governance, threshold sets per segment.

## 3. MVP rules (three, to exercise the language)

| Rule | Type | Definition | Source |
|---|---|---|---|
| `MVP-001` High-value cash deposit | T1 | one `CASH_DEPOSIT` with amount ≥ 10,000 | new, simple |
| `MVP-002` Structuring | T2 | ≥ 3 `CASH_DEPOSIT`s of 8,000–9,999.99 on one account within a rolling 7 days | prototype `R-DEP-01` |
| `MVP-003` High-risk-country wire | T1 | one `WIRE_IN`/`WIRE_OUT` ≥ 5,000 with a country in the high-risk list | prototype `R-DEP-03` (single-wire branch) |

Base points: 20 / 30 / 20, with the severity factor from the prototype (`1 + 0.25 × (ratio − 1)`, capped 1.5).
MVP alert threshold (policy file): **35**, the prototype's value, so MVP results can be compared with the baseline.
(This is an MVP value only; production thresholds are OQ-F13.)

## 4. Functional requirements (MVP subset of `02`)

- **FR-M1** (FR-12, FR-11) A rule file is validated: JSON Schema, only catalogue fields, operators valid for the field type, unique `ruleId`+`version`. Errors carry the JSON path.
- **FR-M2** (FR-10) The language v0 supports: `filters` (field, operator, value or list), `aggregation` (`COUNT`, `SUM`, `MAX` over the filtered transactions), `window` (`ROLLING`, N days, evaluated per FR-29), `groupBy` (`ACCOUNT`), `threshold` (operator, value), `severity` ratio definition. No user code is executed.
- **FR-M3** (FR-33, 35) Evaluation is generated as SQL (no row loops). The same SQL runs on DuckDB in tests and is portable to Spark SQL.
- **FR-M4** (FR-28, 29, 31, 32) Window boundaries, rounding and null handling follow `02` exactly: rolling window `(end(D) − N, end(D)]`; amounts compared at 2 decimals half-up; null never matches.
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
- **MAC-3** (MVP-001) A deposit of 10,000.00 matches; 9,999.99 does not; a non-cash 12,000 does not.
- **MAC-4** (MVP-002) Three cash deposits of 8,000–9,999.99 within 7 days match; two do not; an amount of 7,999.99 or 10,000.00 does not count toward the three.
- **MAC-5** (FR-29) With deposits at exactly end(D)−7 days and at end(D): the earlier one is excluded; the later included.
- **MAC-6** (MVP-003) A wire of 5,000.00 with a high-risk country matches; 4,999.99 does not; a high-risk country on a card transaction does not.
- **MAC-7** (FR-32) A null amount or null country never matches.

Detections, novelty, alerts
- **MAC-8** (FR-M5) The same inputs produce identical `detectionId`s; changing one trigger transaction changes the id.
- **MAC-9** (FR-M6) Structuring on days D and D+1 with the same three deposits: the second detection is suppressed and no alert is created; adding a fourth deposit on D+2 creates a new detection.
- **MAC-10** (FR-M8) A party with two accounts and detections from two rules scoring ≥ 35 gets exactly one alert with both detections; a party scoring 34.9 gets none.
- **MAC-11** (FR-M8) A detection on a joint account is addressed to the primary party; the joint holder is listed with role JOINT; no alert for the joint holder.
- **MAC-12** (FR-M7) Evidence totals equal the sum of the linked transactions; if forced inconsistent, no alert is created and the failure is recorded.

Persistence, runs, outbox
- **MAC-13** (FR-M9) A failure injected between the alert insert and commit leaves no alert, evidence, links or outbox row.
- **MAC-14** (FR-M11) Running the same date twice creates no additional detections, alerts or outbox rows.
- **MAC-15** (FR-M10) A rule with an execution error is recorded; other rules' results exist; run status `PARTIAL`.
- **MAC-16** (FR-M12) `--dry-run` writes no rows.
- **MAC-17** (FR-M13) The stub publishes pending rows once, in order, marks them PUBLISHED, and a simulated failure increments attempts and ends in FAILED after the limit.
- **MAC-18** (FR-M14) No module under `ruleengine/` reads `labels_*` files.

Conformance with the prototype (informational, not pass/fail; differences explained)
- **MAC-19** Run `MVP-002` over Apr–Sep 2026 on the prototype data and compare with the prototype's `R-DEP-01` events: report the number of accounts detected by both, only by the prototype, only by the engine. Expected differences (greedy episodes vs. daily rolling evaluation plus the novelty rule) are listed in section 7. Report precision/recall against the labels in the **evaluation script only** (never the engine).
- **MAC-20** Run all three rules over the six months and report: detections, suppressed, alerts at threshold 35, and the run time. Target: one daily run under 60 seconds on the 1M-row-per-day scale is **not** an MVP criterion; the six months of prototype data (1.25M rows) must complete in under 5 minutes in total.

## 6. Data contracts (MVP)
Input (existing prototype Parquet): `transactions`, `accounts`, `party_account_role`, `counterparties`.
Field catalogue v0: `transaction.amount, txn_type, direction, channel, is_cash, cp_country, account_id, txn_ts, txn_id`; `account.product_type`; `party.id` via role.
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

## 11. Open questions for approval
- [ ] **MQ-1** Approve the MVP scope and the three rules?
- [ ] **MQ-2** Persistence: SQLite for the MVP (no server needed, SQL kept portable to PostgreSQL)? Or do you have PostgreSQL available now?
- [ ] **MQ-3** Package location: `ruleengine/` inside this repository (recommended for speed; move later) or a new repository?
- [ ] **MQ-4** MVP alert threshold 35 and plain-sum scoring acceptable for the MVP only?
- [ ] **MQ-5** Dependencies to add: `duckdb`, `jsonschema`, `pytest` (and `pandas` already required by the prototype). OK?

## 12. Implementation notes
Not applicable until `IMPLEMENTED`.
