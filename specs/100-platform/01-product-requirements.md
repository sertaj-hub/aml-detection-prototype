# Spec 100/01 — Rule Engine: Product Requirements

| | |
|---|---|
| Status | APPROVED (2026-10-02) with assumptions D-1..D-4 below, open to override |
| Author | Claude (with the user, AML SME) |
| Approver | |
| Created | 2026-10-02 |
| Related | `specs/000-baseline/` (reference behaviour), `specs/constitution.md`; next: `02-functional-requirements.md` |

## 1. Context and problem

Banks need transaction monitoring that compliance can configure, govern and explain, and that investigators can
trust. This product is an **independent Financial Crime Transaction Monitoring Rule Engine**: it evaluates
transaction data against configurable detection rules and produces alerts with evidence. Investigation is done by a
separate, **independent Case Management solution** (own repository, deployment, database, release cycle, and
possibly a different team or vendor). The two systems meet only through published, technology-neutral contracts;
neither assumes anything about the other's implementation.

The existing prototype in this repo is the **reference lab**: its behaviour (baseline specs), synthetic data and
evaluation harness define what "correct detection" means, and the platform must reproduce it.

## 2. Scope

**In scope**
- Batch ingestion, validation, normalisation and enrichment of transaction data.
- Declarative, versioned, governed detection rules.
- Batch rule evaluation at transaction-volume scale (millions per day).
- Rule testing and backtesting without producing production alerts.
- Detection records, alert generation, alert evidence and alert persistence.
- Reliable alert-event publication (outbox → Kafka) and a read API for alerts.
- Rule, execution and alert audit.

**Out of scope (version 1)**
- Investigation workflow, assignment, notes, RFI, QA/QC, SAR filing, case closure (Case Management).
- Real-time / streaming detection (reconsidered after batch is proven).
- Supervised or LLM-based detection; complex graph analytics (defined precisely in `04-rule-definition`).
- Sanctions and watch-list screening.

## 3. System context

```
 Transaction sources ──(daily feed)──► Databricks lakehouse (Delta: history)
                                            │
                                            ▼
 Rule definitions (JSON) ◄── PostgreSQL ──► Python batch detection (DSL → SQL;
   (versioned, governed)     (rules, runs,      Spark SQL on Databricks; DuckDB for tests)
                              detections,            │
                              alerts, evidence,      ▼
                              outbox, audit)    Alert generation
                                   │
                         Outbox publisher ──► Kafka: financial-crime.alert.created.v1
                                                     │
                                                     ▼
                                      Case Management (separate, later)
                                                     │ (disposition events back)
                                                     ▼
                                           Rule effectiveness reporting
```

Delivery phases of **this product**: **P1** Python detection engine + PostgreSQL + outbox/Kafka; **P2** Spring Boot
control plane (rule management API, approval workflow, alert API) + React rule UI; **P3** optional disposition
feedback intake. Case Management is **not** a phase of this product; it is a separate solution that integrates
through the contracts in §5.8.

## 4. Glossary

| Term | Meaning |
|---|---|
| Transaction | An immutable financial movement received from a source system, identified by `(sourceSystem, transactionId)` |
| Rule | A declarative definition of suspicious behaviour; has an id and immutable versions |
| Detection | One rule version matching on one entity for one evaluation window, with evidence. Not yet an alert |
| Alert | A unit of work for investigation, created from one or more detections by the alert generation policy |
| Evidence | The inputs, calculated metrics, thresholds and matched conditions that explain a detection |
| Run | One execution of the engine for a business date (or backtest period) against a data snapshot |
| Entity | The subject of a detection: party, account, or a defined group |

## 5. Product requirements

Requirement keywords: **must** = mandatory for version 1; **should** = desirable; priorities in §7.

### 5.1 Data intake
- **PR-1** The engine must ingest transactions from source systems as a batch feed for a business date.
- **PR-2** Every record must be validated against a published transaction schema; invalid records must be rejected to a quarantine with a reason, and must not stop the run.
- **PR-3** Ingestion must be idempotent on `(sourceSystem, transactionId)`; re-delivery must not create duplicate transactions or duplicate detections.
- **PR-4** Each load must produce reconciliation counts (received / accepted / rejected) that are stored with the run.
- **PR-5** Transactions must be normalised to a canonical model (amounts, currency, timestamps in UTC with the original timezone retained, direction, channel, counterparty, country).
- **PR-6** Transaction history needed by rules (13 months at ~1M/day, D-3) must live in the Databricks analytical store, not in the operational database.

### 5.2 Enrichment and reference data
- **PR-7** Transactions must be enriched with the party, account and reference attributes that rules reference (e.g. customer risk rating, expected activity, country risk lists).
- **PR-8** Reference data must be versioned; each run must record the reference-data versions used.
- **PR-9** The set of fields a rule may reference must be a governed field catalogue; a rule referencing an unknown field must fail validation.

### 5.3 Rule definition and governance
- **PR-10** Rules must be defined declaratively (JSON validated by a published schema); no user-supplied code is executed.
- **PR-11** The engine must support rule categories in tiers (precise definitions in `04-rule-definition`): **T1** single-transaction and multi-condition; **T2** aggregation, velocity and distinct-count over a window; **T3** comparison to customer profile (expected activity, KYC attributes); **T4** sequence/pattern (e.g. credits in then funds out); **T5** behaviour against the entity's own history; **T6** relationships (shared counterparties). Version 1 must deliver T1–T3; T4–T6 must be specified and designed for.
- **PR-12** Rules must be versioned; a version is immutable once approved. A change creates a new version.
- **PR-13** Rule lifecycle must include: DRAFT → VALIDATED → TESTED → BACKTESTED → PENDING_APPROVAL → APPROVED → SHADOW (optional) → ACTIVE → SUSPENDED → RETIRED, plus REJECTED and rollback to a previous version.
- **PR-14** The author of a rule version must not be able to approve it (maker-checker).
- **PR-15** Thresholds must be separable from rule logic (threshold sets, optionally per customer segment) so tuning is controlled and auditable; the governance impact of a threshold change is decided in `04-rule-definition`.
- **PR-16** Rules must be tagged with the typology they detect and the products they apply to.

### 5.4 Evaluation
- **PR-17** The engine must evaluate all ACTIVE rule versions in a **daily** run (per business date) and a **monthly** run (per calendar-month close); each rule declares its cadence (DAILY or MONTHLY). A monthly run may be started on any day and always evaluates the previous completed calendar month (D-8).
- **PR-18** Evaluation must be deterministic: the same rule versions, data snapshot and reference-data versions must produce identical detections.
- **PR-19** Each run must record the data snapshot identifier, rule versions, reference-data versions, start/end time, status and counts.
- **PR-20** A failure in one rule must be isolated: it is recorded and reported, and other rules continue.
- **PR-21** A rule in SHADOW state must be evaluated and its detections stored, but must not create alerts.
- **PR-22** The engine must be able to re-run a past business date (reprocessing) without creating duplicate detections or alerts.

### 5.5 Detection, alerts and evidence
- **PR-23** Every match must produce a detection record containing rule id, rule version, entity, window, evidence and run id.
- **PR-24** Detections are produced per rule; alerts are produced **per primary party of the account(s)** per cycle (daily or monthly) by an explicit, versioned alert policy that groups the party's new detections, scores them and applies a threshold (D-4). A detection is consumed by at most one alert. The policy details are defined in `06-alert-management`; the prototype's scoring is the reference.
- **PR-25** An alert must contain at least: alertId, ruleId(s), ruleVersion(s), alertType, party id, account id(s), jurisdiction, businessUnit, severity, detectionTimestamp, createdTimestamp, runId, status.
- **PR-26** Every alert must carry evidence sufficient for an investigator to understand why it fired: input transaction ids, calculated metrics, thresholds, matched conditions, window, rule version, and a plain-English explanation.
- **PR-27** Evidence must reconcile: any amounts or counts shown must be derivable from the linked transactions.
- **PR-28** An alert must identify the entity it concerns, including related parties on the account (joint holders) and the party that initiated the transactions where known.

### 5.6 Persistence and distribution
- **PR-29** Alerts, evidence and the outbox record must be persisted in one database transaction.
- **PR-30** The database is the system of record for alerts; Kafka only distributes events.
- **PR-31** The outbox publisher must publish `financial-crime.alert.created.v1` at least once; every event must carry a unique `eventId` so consumers can deduplicate; publication failures must be retried and visible.
- **PR-32** Events must not contain unnecessary sensitive data; consumers must be able to fetch full alert detail through an API.
- **PR-33** Event contracts must be versioned and backward compatible within a major version.

### 5.7 Testing and backtesting
- **PR-34** A user must be able to test a rule version on a chosen data sample and see match counts and sample evidence, without producing production alerts.
- **PR-35** A user must be able to backtest a rule version over a historical period and see volume, matches, potential alerts and run time; results must be isolated from production alerts.
- **PR-36** The engine must be able to compare two rule versions over the same period (alert volume added/removed).
- **PR-37** When labelled data exists (the synthetic reference lab), backtests should report precision and recall.

### 5.8 Integration with independent consumers (Case Management)
- **PR-38** The engine must not depend on any consumer. It must run, alert and publish with zero, one or many consumers, and must not share a database, libraries, deployment or release schedule with Case Management.
- **PR-38a** Integration must use only published, technology-neutral contracts: a versioned Kafka event schema (JSON Schema or Avro in a schema registry) and a versioned REST API described by OpenAPI. No consumer-specific fields or logic may appear in the engine.
- **PR-38b** The alert read API must allow a consumer to fetch full alert detail, evidence and linked transactions by `alertId`, and to list/replay alerts from a given point (so a consumer that was down or is newly built can catch up).
- **PR-38c** Contracts must be published with example payloads and a consumer-contract test suite that either side can run in its own pipeline.
- **PR-39** After the alert is handed off, investigation state is owned by the consuming system. The engine keeps detection state only and must not model cases, assignments or decisions.
- **PR-40** The engine may accept an optional alert-disposition event (e.g. closed false positive, escalated, SAR filed) from any consumer that chooses to send one, using a published contract, and use it only for rule-effectiveness reporting, never to change detection results. The engine must work fully without it.

### 5.9 Audit and security
- **PR-41** The engine must audit rule lifecycle actions, runs, alert creation, publication, publication failure and replay, recording who/what/when and the before/after for changes.
- **PR-42** Audit records must be append-only.
- **PR-43** Access must be role-based (viewer, author, validator, approver, administrator, operator, auditor) with authentication through the enterprise identity provider.
- **PR-44** Personal data must be handled according to retention and masking rules defined in `09-non-functional-requirements`.

### 5.10 Observability
- **PR-45** The engine must expose per-run and per-rule metrics: records processed, matches, match rate, alerts created, execution time, errors, outbox backlog.
- **PR-46** A run must fail loudly (alert to operators) if data completeness or reconciliation checks fail.

## 6. Acceptance criteria

- **AC-1** (PR-3) Given a feed containing the same `(sourceSystem, transactionId)` twice, When it is ingested, Then one transaction is stored and no duplicate detection results.
- **AC-2** (PR-2) Given a feed with 100 records of which 3 fail schema validation, When ingested, Then 97 are accepted, 3 are quarantined with reasons, and the run completes.
- **AC-3** (PR-12) Given an ACTIVE rule version, When a user changes it, Then a new version is created and the ACTIVE version's definition is unchanged.
- **AC-4** (PR-14) Given user U authored version V, When U attempts to approve V, Then the action is refused and audited.
- **AC-5** (PR-18) Given the same rule versions, data snapshot and reference data, When the run is executed twice, Then detections are identical.
- **AC-6** (PR-22) Given business date D already processed, When D is re-run, Then no duplicate detections or alerts exist and the replay is audited.
- **AC-7** (PR-20) Given rule R fails with an error and rule S is healthy, When the run executes, Then S's detections are produced, R's failure is recorded, and the run status is PARTIAL.
- **AC-8** (PR-21) Given a rule in SHADOW, When it matches, Then a detection is stored and no alert or outbox event is created.
- **AC-9** (PR-26/27) Given an alert, When its evidence is read, Then every amount and count equals the sum/count of the linked transactions, with rule version and threshold shown.
- **AC-10** (PR-29) Given a failure after the alert insert and before commit, When the transaction aborts, Then no alert, evidence or outbox row exists.
- **AC-11** (PR-31) Given Kafka is unavailable, When alerts are created, Then outbox events stay pending, are published after recovery, and carry unique eventIds.
- **AC-12** (PR-34/35) Given a test or backtest, When it completes, Then no production alert or outbox event is created.
- **AC-13** (PR-38) Given no consumer is running, When the engine runs, Then runs, alerts and publication complete normally, and a consumer started later can retrieve every alert through the API and/or replay from Kafka.
- **AC-15** (PR-38a/c) Given the published contract and example payloads, When an independent consumer (a test stub written without access to the engine's code) validates them, Then all events and API responses conform.
- **AC-16** (PR-38b) Given a consumer that was offline for three runs, When it requests alerts since its last position, Then it receives each alert once, in order, with evidence.
- **AC-14** (reference lab) Given the synthetic dataset and the 15 reference rules expressed in the rule definition language, When the engine runs the six months, Then results meet the conformance criteria set in `05-rule-evaluation` (alert volume, productive rate and recall within agreed tolerance of `CLAUDE.md` §6, with differences explained).

## 7. Priorities

| Priority | Requirements |
|---|---|
| Must (P1) | PR-1–10, 12, 17–20, 22–23, 25–27, 29–31, 34–35, 38, 38a, 38c, 39, 41–42, 45–46 |
| Must (P2) | PR-13–16, 24, 36, 43 |
| Must (P3) | PR-38b, 40 (optional feedback intake) |
| Should | PR-21, 28, 32–33, 37, 44 |

(Priorities are a first proposal for review.)

## 8. Success criteria

1. The reference rules run in batch over the synthetic six months and conform to the baseline (AC-14).
2. Every alert names its rule version and carries evidence that reconciles (AC-9).
3. Re-running a business date changes nothing (AC-6).
4. No alert is lost between database and Kafka (AC-10, AC-11).
5. A rule can be tested and backtested without any production effect (AC-12).
6. An independent Case Management solution can be built against the published contracts alone (AC-13, AC-15, AC-16).
7. Performance target (set in `09-non-functional-requirements`): a daily run over ~1M new transactions with 13 months of history finishes within the agreed batch window; a monthly run likewise.

## 9. Data contracts

Detailed in later specs: canonical transaction (`03-domain-model`), rule definition (`04`), alert and evidence
(`06`), `AlertCreated` and disposition events (`07`), APIs (`08`).

## 10. AML and regulatory considerations
- Typologies: initially the 18 in the prototype (structuring, rapid movement, funnel/mule, high-risk geography, dormant reactivation, cash-profile mismatch, card/loan abuse, cross-product cash-to-credit, joint-holder abuse, thin spread).
- Regulator expectations: documented rule logic, change control with approval, tuning evidence, complete audit trail, ability to reproduce an alert (PR-18, PR-19), and data-quality controls (PR-4, PR-46).
- Data must not include real customer data in non-production environments.

## 11. Model risk impact
Introducing the platform must not silently change what is detected: AC-14 requires explained conformance with the
baseline. Any intentional difference (e.g. rules redefined set-based) is recorded as a rule-level deviation with
before/after metrics.

## 12. Constitution check
| Principle | Status |
|---|---|
| 1 Explainability | Respected: PR-23, 26, 27 |
| 2 Reproducibility | Respected: PR-18, 19, 22 |
| 3 Labels out of detection | Respected: labels used only for backtest evaluation (PR-37), never in rule evaluation |
| 4 No silent change | Respected: AC-14, §11 |
| 5 Honest evaluation | To be detailed in `05` and `13` (out-of-time backtests) |
| 6 Regulator-readable | Respected: specs in Markdown with traceable requirement IDs |
| 7 Synthetic data only | Respected in non-production; production data handling in `09` |
| 8 Simplicity | Batch-first; streaming deferred; monolith-first modules |

## 12a. Decisions (confirmed by the user, 2026-10-02)

| ID | Decision | Resolves | Affects |
|---|---|---|---|
| D-1 | Batch only in v1, two cadences: **daily** (per business date) and **monthly** (per calendar-month close). Intraday/streaming deferred | OQ-2 | 02, 05 |
| D-2 | Lakehouse = **Databricks** (Delta tables, Spark SQL / PySpark). DuckDB is used only for local development and automated tests on the same SQL subset | OQ-1 | 05, 09 |
| D-3 | Volume: **~1 million transactions/day, 13 months of history** (~400 million rows) | OQ-3 | 09 |
| D-4 | **Detections are per rule (event per rule); alerts are per primary party of the account.** Detections for a party in a cycle are consolidated into one alert (daily cycle, monthly cycle). A detection belongs to at most one alert | OQ-4 | 02, 06 |
| D-5 | Initial alert thresholds: **50 for the daily cycle, 100 for the monthly cycle** (policy parameters, versioned and governed). The **daily cycle is the business date of the transaction(s)**, not the run date | OQ-F9 | 02, 06 |
| D-6 | An alert is created per primary party per business date; the engine does **not** merge with or link to other open alerts. Consolidation of alerts into cases is the consumer's job and is out of scope | OQ-F10 | 02, 06 |
| D-7 | Unconsumed (below-threshold) daily detections are included in the monthly cycle, then **expire** | OQ-F11 | 02 |
| D-8 | The monthly run may be started on any day; it always evaluates the **previous completed calendar month** | OQ-F12 | 02 |

## 13. Open questions
- [x] **OQ-1** Lakehouse: Databricks (D-2).
- [x] **OQ-2** Cadence: daily and monthly (D-1).
- [x] **OQ-3** Volumes: ~1M/day, 13 months (D-3).
- [x] **OQ-4** Alert policy: event per rule, alert per primary party (D-4).
- [ ] **OQ-5** Does a threshold change require full re-approval, or a lighter governed path?
- [ ] **OQ-6** Which jurisdictions / business units, and is the engine multi-tenant?
- [ ] **OQ-7** Who owns reference data (country risk lists, customer risk rating) and how fresh must it be?
- [ ] **OQ-8** Who owns the integration contract (the engine team, a shared architecture group)? Which alert fields must the Case Management team have, and who signs off contract changes?
- [ ] **OQ-11** Which schema format and registry will Kafka contracts use (JSON Schema vs Avro)? Is there an enterprise standard?
- [ ] **OQ-12** Is a single consumer expected, or several (e.g. reporting, regulatory feeds) that need the same events?
- [ ] **OQ-9** Retention periods for detections, alerts and audit (transactions: 13 months online per D-3; archive beyond that?)
- [ ] **OQ-13** Databricks specifics: workspace/Unity Catalog layout, job compute (serverless vs clusters), how Python jobs reach PostgreSQL, network and secrets standards.
- [ ] **OQ-10** Should the prototype's ML anomaly models become a rule-visible `MODEL_SCORE` condition in a later version?

## 14. Implementation notes
Not applicable until `IMPLEMENTED`.
