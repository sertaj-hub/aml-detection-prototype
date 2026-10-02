# Spec NNN — <Title>

| | |
|---|---|
| Status | DRAFT |
| Author | |
| Approver | |
| Created | YYYY-MM-DD |
| Related | <other specs, rule IDs, model IDs> |

## 1. Context and problem
Why this change is needed. What goes wrong today, for whom (investigator, model validator, operations).

## 2. Scope
**In scope**
-

**Out of scope**
-

## 3. Functional requirements
Numbered, each one testable and unambiguous. Use "must" for requirements.

- **FR-1** The system must …
- **FR-2** …

## 4. Acceptance criteria
Given / When / Then. Each AC maps to at least one test named after it (`test_ac1_...`).

- **AC-1** (FR-1) Given … When … Then …
- **AC-2** (FR-1) Given … When … Then …

Include boundary cases (exactly at threshold, one below) and a not-fire case for every fire case.

## 5. Data contracts
Tables / columns read and written. New or changed columns with type and meaning.

| Table | Column | Type | Change | Meaning |
|---|---|---|---|---|

## 6. AML and regulatory considerations
- Typologies affected:
- Expected effect on false positives / false negatives:
- Explainability: what the investigator will see:
- Data the rule/model must not use (e.g. labels):

## 7. Model risk impact
Does this change the alerted population? Expected before/after on the baseline metrics
(alerts, productive rate, party recall, scheme recall). If none expected, say "none" and how it is verified.

## 8. Constitution check
For each principle in `constitution.md`: respected / not applicable / exception (with reason).

## 9. Open questions
- [ ] …

## 10. Implementation notes
Filled in when `IMPLEMENTED`: files changed, tests added, actual metric changes, deviations from the spec.
