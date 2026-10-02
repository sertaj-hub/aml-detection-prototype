# Spec 100/02 — Rule Engine: Functional Requirements

| | |
|---|---|
| Status | DRAFT |
| Author | Claude (with the user, AML SME) |
| Approver | |
| Created | 2026-10-02 |
| Related | `01-product-requirements.md` (PR-n, confirmed decisions D-1..D-4), `specs/000-baseline/` |

Defines **exact behaviour** of the engine. Later specs define the models and contracts these behaviours use:
`03` domain model, `04` rule definition, `05` evaluation internals, `06` alert management, `07` events, `08` APIs.
Every FR traces to a product requirement (PR) and has acceptance criteria in §4.

## 1. Context and problem
`01` says *what* the product must do. This spec says *how it must behave* at each step, precisely enough to write
tests before code: intake, run orchestration, evaluation semantics, detections, alerts, outbox, testing, reprocessing.

## 2. Scope
In scope: batch behaviour of the Rule Engine (P1) and the lifecycle/governance behaviour the engine enforces.
Out of scope here: rule JSON syntax (`04`), table DDL (`05`/`03`), event and API payloads (`07`/`08`), UI (later).

## 3. Functional requirements

### 3.1 Intake (PR-1…6)
- **FR-1** A source delivers a **batch** for `(sourceSystem, businessDate)` as files in a landing area (cloud storage mounted in Databricks) plus a manifest containing file names, row counts and checksums. The engine must not read a batch whose manifest is missing or whose checksums fail.
- **FR-2** Each record is validated: required fields present, types valid, amount > 0, currency known, timestamp parseable, direction ∈ {CR, DR}, account and party resolvable. A failed record goes to quarantine with `reason_code` and the original payload; the batch continues.
- **FR-3** Idempotency: a transaction is identified by `(sourceSystem, transactionId)`. Re-delivery of an identical record is ignored. A re-delivery with **different content** is a **correction**: stored as a new revision, flagged, and counted in the reconciliation; the original is retained.
- **FR-4** Reconciliation per batch: `received = accepted + corrected + duplicate + quarantined`; the manifest row count must equal `received`. Any mismatch fails the batch.
- **FR-5** Transactions are normalised to the canonical model (`03`): UTC timestamp plus original offset, amount as exact decimal in transaction currency plus converted amount in the reporting currency with the rate date, standard direction, channel, counterparty and country codes.
- **FR-6** Late arrivals: a transaction whose `transactionTs` is older than the `businessDate` is accepted into history. Windows that include it are **restated** for up to `restatementDays` (default 3, configurable) back; older late data is stored and reported but does not trigger restatement.
- **FR-7** Completeness gate: before detection, the run checks (a) every expected source delivered its batch, (b) quarantine rate ≤ configured limit (default 2%), (c) row count within tolerance of the trailing 7-day average. A failed gate stops the run (status `BLOCKED`) and notifies operators; an operator may override with a recorded reason.

### 3.2 Enrichment (PR-7…9)
- **FR-8** Enrichment attaches party attributes (risk rating, segment, expected activity, PEP flag), account attributes (product, open date, branch/jurisdiction, status), account roles (primary/joint/etc.) and country/MCC risk classifications **as of the transaction's business date**, not as of today.
- **FR-9** Reference data (country risk lists, expected-activity profiles, segments) is versioned with an effective date; each run records the versions used.
- **FR-10** A transaction whose party or account cannot be resolved is quarantined (FR-2), not silently enriched with defaults.
- **FR-11** A rule may reference only fields in the **field catalogue**; the catalogue states each field's type, source, allowed operators and nullability.

### 3.3 Rule lifecycle enforced by the engine (PR-10…16)
- **FR-12** A rule version is validated against the DSL schema and the field catalogue; validation produces a list of errors with JSON paths.
- **FR-13** Allowed transitions: DRAFT→VALIDATED→TESTED→BACKTESTED→PENDING_APPROVAL→APPROVED→(SHADOW)→ACTIVE→SUSPENDED→(ACTIVE)|RETIRED; PENDING_APPROVAL→REJECTED→DRAFT (new version). Any other transition is refused and audited.
- **FR-14** A version in status APPROVED or beyond is immutable (definition, thresholds, applicability).
- **FR-15** Approval requires an approver different from the author and from the person who last edited; the approver's identity, time and comment are stored.
- **FR-16** Activation has an **effective date**. A run for business date D uses the versions ACTIVE on D (not the versions active when the run happens). This makes reruns reproducible.
- **FR-17** At most one version of a rule id is ACTIVE for a given date; activating a new version retires the previous version from that effective date.
- **FR-18** Suspension takes effect from the next run; an emergency suspend takes effect for any run not yet started. Rollback re-activates a prior approved version through the same governed path.
- **FR-19** A threshold set belongs to a rule version. Changing thresholds produces a new threshold-set version with its own approval; whether the rule logic version also changes is decided in `04` (OQ-5).

### 3.4 Run orchestration (PR-17…22, 45, 46)
- **FR-20** A **run** is created per `(cadence, period, mode)` where cadence ∈ {DAILY (period = business date), MONTHLY (period = calendar month, started after the month's last daily run succeeded)} and mode ∈ {PRODUCTION, BACKTEST, TEST}. Only one PRODUCTION run per cadence and period may be RUNNING; a second request is queued or refused. A DAILY run evaluates rules with cadence DAILY; a MONTHLY run evaluates rules with cadence MONTHLY and performs the monthly alert cycle (FR-41).
- **FR-21** At start, the engine writes a **run manifest**: rule versions and threshold sets, field-catalogue version, reference-data versions, data snapshot identifier (Delta table versions of every input table), code version, parameters. The manifest is immutable.
- **FR-22** Run states: PENDING → RUNNING → SUCCEEDED | PARTIAL | FAILED | BLOCKED | CANCELLED. PARTIAL means at least one rule failed and at least one succeeded.
- **FR-23** Rules are evaluated independently. A rule error (invalid data, query failure, timeout) is recorded with rule id, version, error and query id; other rules continue (PR-20). The run is PARTIAL and the failed rules are listed.
- **FR-24** Each rule execution records: start/end, rows scanned, detections produced, alerts created, status. These feed metrics (PR-45).
- **FR-25** Determinism: for the same manifest, detections are identical, including ordering of evidence lists (sorted by transaction timestamp then id) and rounding rules (see FR-31).
- **FR-26** Re-running a business date (**reprocessing**) creates a new run attempt linked to the previous one. Identical results create no new detections or alerts (see FR-37). Differences (e.g. restated data, a rule activated retrospectively by an operator) produce new detections marked `origin = REPROCESS`; they create alerts only if the alert policy allows (default: yes, flagged).
- **FR-27** A run completes only after all rule executions finished and the outbox rows for its alerts are committed.

### 3.5 Evaluation semantics (PR-11, 18)
- **FR-28** **Evaluation date**: a run for business date D evaluates the data with `transactionTs` ≤ end of D in the **bank's operating timezone** (a configuration value, default UTC).
- **FR-29** **Rolling windows** of N hours/days (DAILY cadence) are evaluated at the end of D over `(end(D) − N, end(D)]` (start exclusive, end inclusive); the maximum rolling window is bounded by the 13-month history (D-3). **Calendar-month windows** (MONTHLY cadence) are evaluated once, by the MONTHLY run, over `[first instant of month, last instant of month]`; they never fire twice for the same month and entity.
- **FR-30** **Aggregation functions** v1: COUNT, COUNT DISTINCT, SUM, MIN, MAX, AVG, and the ratio of two aggregates over (possibly different) filters/windows. Grouping entity: PARTY, ACCOUNT, or a defined group. A group-by key with NULL is excluded and counted in run metrics.
- **FR-31** Money arithmetic uses exact decimals; comparisons are on the reporting-currency amount rounded to 2 decimals half-up. Ratios are compared at full precision and displayed to 1 decimal.
- **FR-32** Operators v1: `=, !=, <, <=, >, >=, IN, NOT IN, BETWEEN, IS NULL, IS NOT NULL, CONTAINS (set membership)`; boolean `AND/OR/NOT` with explicit grouping. Null in a comparison evaluates to false (never to true).
- **FR-33** Applicability: a rule applies only to transactions/entities matching its scope (product, channel, segment, jurisdiction, business unit). Out-of-scope data is not evaluated and not counted.
- **FR-34** Profile-relative conditions (e.g. cash vs the party's expected monthly cash) use the profile value as of D with a configured floor to avoid division by near-zero (the floor is a rule parameter, never a hidden constant).
- **FR-35** Joint/secondary parties: the rule declares the **entity** it detects on. When the entity is ACCOUNT or a group, the detection lists every party on the account with roles, and the initiating party of each trigger transaction where known.

### 3.6 Detections and novelty (PR-23, 24)
- **FR-36** A **detection** is created when a rule's conditions hold for an entity at the end of D. It stores: rule id, version, threshold set, entity, window start/end, evaluation date, run id, trigger transaction ids, metrics, matched conditions, evidence (FR-40), `origin` (SCHEDULED/REPROCESS).
- **FR-37** **Novelty rule** (prevents the daily re-firing of rolling windows and monthly re-alerting seen in the prototype): a detection is **new** only if it contains at least one trigger transaction that is **not** a trigger transaction of any earlier detection of the same rule id and entity (any version). A matching detection with no new trigger transaction is stored as `SUPPRESSED_NO_NEW_EVIDENCE`, linked to the earlier detection, and creates no alert.
- **FR-38** A detection with a **new** trigger set that overlaps an earlier detection is `NEW_WITH_OVERLAP`; it creates an alert that references the earlier alert(s) as `relatedAlerts`.
- **FR-39** Detection identity is deterministic: `detectionId = hash(ruleId, entityKey, sorted trigger txn ids, windowEnd)`. The same inputs always give the same id (supports idempotency and replay).
- **FR-40** **Evidence** has structured content (inputs, metrics with values and thresholds, window, rule version, matched conditions, trigger/context transactions with their contribution) and a generated plain-English explanation. Every number in the explanation must be derivable from the linked transactions (PR-27); the engine verifies this before persisting, and refuses to create the alert if it does not reconcile (the failure is recorded).

### 3.7 Alert generation (PR-24, 25, 28)
- **FR-41** **Alert policy (D-4).** Detections are events per rule. For each **cycle** the engine groups the cycle's *new, unconsumed* detections (FR-36, FR-37) by the **primary party** of the detection's account(s) and creates **one alert per primary party per cycle**, containing every grouped detection as a child with its evidence.
  - Cycles: the DAILY run forms the *daily cycle* from DAILY-cadence detections of that business date. The MONTHLY run forms the *monthly cycle* from MONTHLY-cadence detections of the month **plus any DAILY-cadence detections of that month that did not produce an alert** (below threshold), so sub-threshold behaviour accumulates.
  - **Score**: each detection earns *points* = rule-version base points × severity factor (from the threshold set), with the repeat discount, cross-product bonus and KYC bonus defined by the alert policy version (reference: baseline `003-scoring-and-alerts.md`). The party's cycle score is the sum.
  - **Threshold**: an alert is created only when the party's cycle score ≥ the policy threshold. Policy parameters (points, factors, bonuses, threshold) live in a versioned, governed **alert policy** (approval as for rules); the policy version is recorded in the run manifest and on the alert.
  - **Consumption**: a detection belongs to at most one alert. Detections that did not reach the threshold stay unconsumed and are carried to the monthly cycle (above), after which they expire.
  - Secondary holders (joint, authorised user, co-borrower) are listed on the alert with roles and the initiating party; the alert is addressed to the primary party (FR-35).
  - SHADOW-rule detections never enter an alert (FR-45).
- **FR-42** Severity comes from the rule version (and optionally the threshold set band, e.g. HIGH when ratio ≥ 2×). The alert records the rule's severity and the band that determined it.
- **FR-43** `alertId` is generated by the engine as a unique, non-reusable identifier (e.g. `ALT-<cycle>-<sequence>`); it is **not** renumbered on reruns. Replay idempotency uses the deterministic key `alertKey = hash(primaryPartyId, cycleType, cycleId, alertPolicyVersion)` plus the `detectionId`s (FR-39): a rerun that yields the same detections for the same party-cycle returns the existing alert.
- **FR-44** Alert fields are set per PR-25. `status` in the engine is `GENERATED` → `PUBLISHED`; investigation statuses belong to the consumer.
- **FR-45** An alert for a SHADOW rule is **never** created; its detections are stored with `shadow = true` (PR-21).

### 3.8 Persistence and publication (PR-29…33)
- **FR-46** In one database transaction the engine writes: alert, alert evidence, alert↔detection links, outbox event. Either all are committed or none.
- **FR-47** Outbox row: `eventId` (UUID), `aggregateId` (alertId), `eventType`, `payload`, `status` (PENDING, PUBLISHED, FAILED), `attempts`, `nextAttemptAt`, timestamps.
- **FR-48** The publisher claims PENDING rows (`FOR UPDATE SKIP LOCKED`), publishes to Kafka with the key `partyId` (so one party's events stay ordered), marks PUBLISHED only after broker acknowledgement, and retries with exponential backoff. After `maxAttempts` (default 10) the row is `FAILED`, visible on the dashboard and alertable. Operators can requeue.
- **FR-49** Delivery is **at-least-once**; consumers deduplicate by `eventId`.
- **FR-50** A catch-up API serves alerts in a stable order (`createdTimestamp, alertId`) from a cursor (PR-38b).
- **FR-51** Events carry identifiers and classification only (no names, no full transaction list); consumers fetch detail through the API (PR-32).

### 3.9 Testing and backtesting (PR-34…37)
- **FR-52** TEST runs a rule version over a chosen sample (date range, party sample, or fixture dataset). BACKTEST runs it over a historical period. On Databricks, both write only to a separate `sandbox` catalog/schema (and a sandbox PostgreSQL schema); they never write alerts, outbox events or production detections, and the code path must be unable to (enforced by a separate database role without write access to production tables).
- **FR-53** TEST/BACKTEST results: transactions evaluated, entities evaluated, matches, would-be alerts (after the novelty rule and policy), runtime, sample evidence, distribution by month and segment.
- **FR-54** **Version comparison**: for versions A and B over the same period report alerts only in A, only in B, in both, and the volume delta.
- **FR-55** Backtesting honours the same effective-dated reference data, thresholds and restatement semantics as production.
- **FR-56** Rule **fixtures**: a rule version may carry example datasets (inputs) with expected outcomes (match/no match, expected metrics); fixtures run automatically at validation and must pass before TESTED.
- **FR-57** When labelled data is supplied (synthetic reference lab), BACKTEST also reports precision and recall; labels are read only by the reporting step, never by rule evaluation.

### 3.10 Audit and operations (PR-41…46)
- **FR-58** Audit events are appended for: rule create/edit/transition/approve/reject; run create/start/finish/override; rule execution failure; alert generated; outbox published/failed/requeued; reprocess; user access denials. Each has actor (user or service), timestamp, entity, action, before/after (or hash), and run id where relevant.
- **FR-59** Audit rows cannot be updated or deleted by the application role; retention follows `09`.
- **FR-60** Metrics per run and per rule: transactions read, detections, suppressed, alerts, errors, duration; outbox pending/failed counts and oldest pending age. A run that exceeds its expected duration (configurable) raises an operator alert.

## 4. Acceptance criteria

Intake
- **AC-1** (FR-1) Given a batch whose checksum does not match the manifest, When the engine starts intake, Then the batch is rejected unread and an operator alert is raised.
- **AC-2** (FR-2/4) Given a 1,000-record batch with 5 invalid records, When ingested, Then 995 are accepted, 5 quarantined with reason codes, and `received = accepted + corrected + duplicate + quarantined`.
- **AC-3** (FR-3) Given a record re-delivered identically, Then it is counted as duplicate and nothing changes. Given re-delivered with a different amount, Then a new revision is stored, flagged as correction, and the original is retained.
- **AC-4** (FR-6) Given a transaction dated 2 days before the business date arrives today, When the next run executes, Then windows containing it are restated; given one dated 10 days earlier, Then it is stored and reported but triggers no restatement.
- **AC-5** (FR-7) Given source B did not deliver, When the run starts, Then it is BLOCKED; an operator override with a reason lets it proceed and the override is audited.

Enrichment
- **AC-6** (FR-8/9) Given a party's risk rating changed from LOW to HIGH on date X, When a run evaluates a transaction dated before X, Then the enrichment shows LOW, and the run manifest lists the reference-data version used.
- **AC-7** (FR-10/11) Given a rule referencing a field not in the catalogue, Then validation fails with the JSON path; given a transaction with an unknown account, Then it is quarantined.

Lifecycle
- **AC-8** (FR-13) Given a rule in DRAFT, When a user tries to activate it, Then the transition is refused and audited.
- **AC-9** (FR-14/15) Given an APPROVED version, When any user edits its definition, Then the edit is refused; given the author tries to approve, Then refused.
- **AC-10** (FR-16/17) Given v1 ACTIVE until D−1 and v2 ACTIVE from D, When business date D−1 is rerun, Then v1 is used; for D, v2.
- **AC-11** (FR-19) Given a threshold change, Then a new threshold-set version is created and the previous one is unchanged.

Runs and evaluation
- **AC-12** (FR-20/21) Given a PRODUCTION run is RUNNING for date D, When another is requested for D, Then it is queued or refused; and the manifest of the first run is immutable.
- **AC-13** (FR-23) Given rule R has an invalid query and S is healthy, Then the run is PARTIAL, S's results exist, R's error is recorded with the query id.
- **AC-14** (FR-25) Given the same manifest, Then two runs produce the same detection ids and the same evidence ordering.
- **AC-15** (FR-29) Given a rolling 24h window and a transaction at exactly end(D) − 24h, Then it is excluded; one at exactly end(D) is included. Given a monthly rule, Then it fires once for the month and never twice.
- **AC-16** (FR-30/32) Given a group key that is NULL, Then the group is excluded and counted; given `amount > 10000` and amount NULL, Then it does not match.
- **AC-17** (FR-31) Given a sum of 9,999.995 in reporting currency, Then it is rounded to 10,000.00 and compared at that value; given a ratio 0.7999 against 0.8, Then it does not match.
- **AC-18** (FR-35) Given a joint account where party B initiated the trigger transactions, Then the detection lists both parties with roles and marks B as initiator.

Detections and alerts
- **AC-19** (FR-37) Given a rolling-7-day rule matched on day D with transactions T1-T3, When day D+1 matches the same T1-T3, Then the detection is `SUPPRESSED_NO_NEW_EVIDENCE` and no alert is created.
- **AC-20** (FR-37/38) Given day D+2 matches T2, T3, T4, Then a new alert is created with `relatedAlerts` pointing to the first.
- **AC-21** (FR-39) Given identical inputs, Then `detectionId` is identical; given one different trigger transaction, Then it differs.
- **AC-22** (FR-40) Given evidence where the stated total differs from the sum of the linked transactions, Then no alert is created and the reconciliation failure is recorded.
- **AC-23** (FR-26) Given a rerun of D with unchanged data, Then no new detections or alerts; given restated data adding T5, Then a new detection `origin = REPROCESS` is created and its alert is flagged.
- **AC-24** (FR-45) Given a SHADOW rule matching, Then a detection with `shadow = true` exists, and no alert or outbox row.
- **AC-35** (FR-41) Given a party P with accounts A1 and A2 and detections from three different rules in one daily cycle with score ≥ threshold, Then exactly one alert is created for P containing three child detections, and each detection is marked consumed by it.
- **AC-36** (FR-41) Given joint account J with primary P and joint holder Q, and a detection on J, Then the alert is addressed to P and lists Q with role JOINT; no alert is created for Q.
- **AC-37** (FR-41) Given a party whose daily scores are 20, 18 and 22 (threshold 35) on three days of the month, When the MONTHLY run executes, Then the three unconsumed daily detections are included and the monthly alert is created if the combined score ≥ threshold.
- **AC-38** (FR-41) Given a detection already consumed by a daily alert, Then it is not counted again in the monthly cycle.
- **AC-39** (FR-41/43) Given the daily run for D is repeated with identical detections, Then the existing alert (same `alertKey`) is returned and no second alert or outbox event is created.

Persistence
- **AC-25** (FR-46) Given a failure injected after the alert insert and before commit, Then no alert, evidence, link or outbox row exists.
- **AC-26** (FR-48/49) Given Kafka is down for 2 hours, Then rows stay PENDING with growing attempts, publish after recovery, and a duplicate publish carries the same `eventId`; given `maxAttempts` exceeded, Then the row is FAILED and visible.
- **AC-27** (FR-48) Given two alerts for the same party, Then their events share the Kafka key and are delivered in order.
- **AC-28** (FR-50) Given a consumer cursor at alert N, Then the API returns alerts after N in stable order with no gaps or repeats.

Testing and backtesting
- **AC-29** (FR-52) Given a BACKTEST run, Then the production alert, detection and outbox tables are unchanged, and an attempt to write to them with the sandbox role fails.
- **AC-30** (FR-54) Given versions A and B over a month, Then the report shows alerts only in A, only in B, in both, and the delta.
- **AC-31** (FR-56) Given a rule with a failing fixture, Then it cannot reach TESTED.
- **AC-32** (FR-57) Given labels supplied, Then precision and recall are reported, and rule evaluation code has no read access to the label tables.

Audit and operations
- **AC-33** (FR-58/59) Given any lifecycle action, Then an audit row with actor, time and before/after exists; an update or delete on audit rows by the application role fails.
- **AC-34** (FR-60) Given a run that exceeds its expected duration, Then an operator alert is raised.

## 5. Data contracts
Introduced here and defined in later specs: transaction, quarantine, detection, alert, evidence, run manifest,
outbox, audit (`03`, `05`, `06`); events (`07`); APIs (`08`).

## 6. AML and regulatory considerations
- **Novelty rule (FR-37)** is the main anti-noise control. It must be reviewed with the SME: it suppresses a repeat of the same behaviour, so the SME must confirm that "no new transaction" means "no new risk" in each typology (e.g. persistent structuring should arguably raise severity, not vanish).
- Effective-dated reference data (FR-8/9/16) lets an examiner reproduce why an alert was or wasn't raised on a given date.
- Restatement (FR-6) addresses evasion by delayed posting.
- Calendar-window firing (FR-29) addresses the prototype's month-end split (baseline Q-09) only partly; rolling windows are the recommended default for structuring-type typologies.

## 7. Model risk impact
No change to the alerted population is claimed. Differences versus the prototype baseline come from: novelty rule
(fewer repeat alerts), restatement, effective-dated enrichment, set-based window semantics. Each is measured in the
conformance run (`01` AC-14) and recorded per rule.

## 8. Constitution check
| Principle | Status |
|---|---|
| 1 Explainability | FR-40 (reconciling evidence, plain-English) |
| 2 Reproducibility | FR-16, 21, 25, 39 (effective dating, manifest, deterministic ids) |
| 3 Labels out of detection | FR-52, 57 (role-level enforcement) |
| 4 No silent change | §7 and conformance measurement |
| 5 Honest evaluation | FR-55, 57 (out-of-time backtests, labelled metrics) |
| 6 Regulator-readable | Numbered FR/AC with traceability to PR |
| 7 Synthetic data only | Non-production only; production in `09` |
| 8 Simplicity | Batch, daily + monthly cycles; novelty rule instead of full case-linking logic |

## 9. Open questions
- [ ] **OQ-F1** Novelty rule: should repeated behaviour with no new transactions ever re-alert (e.g. after N days or when severity would rise)? SME decision per typology.
- [ ] **OQ-F2** Restatement window default (3 days) and whether restated alerts should be tagged for investigator attention.
- [ ] **OQ-F3** Bank operating timezone and business-day calendar (holidays) for calendar windows.
- [ ] **OQ-F4** Are corrections to transactions (FR-3) expected from source systems, and how common?
- [ ] **OQ-F5** Completeness gate thresholds (quarantine ≤2%, row-count tolerance): confirm with data owners.
- [ ] **OQ-F6** Reporting currency and FX rate source/rate date convention.
- [ ] **OQ-F7** Should alert severity be able to rise on repeat behaviour (instead of suppression)?
- [x] **OQ-F8** Resolved by D-4: alerts are per primary party per cycle.
- [ ] **OQ-F9** Daily-cycle alert threshold: same as the monthly threshold (prototype: 35), or different because a day is a smaller sample? Needs backtest evidence.
- [ ] **OQ-F10** Should a daily alert for a party with an already-open alert from the same month be linked or merged (the engine does not know case state)? Default: separate alert linked via `relatedAlerts`.
- [ ] **OQ-F11** Expiry of unconsumed sub-threshold detections after the monthly cycle: confirm they do not carry into later months.
- [ ] **OQ-F12** Monthly run timing (e.g. business day +2) and treatment of late data after it (restatement, FR-6).

## 10. Implementation notes
Not applicable until `IMPLEMENTED`.
