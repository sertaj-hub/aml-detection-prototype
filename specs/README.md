# Specs

Every behaviour change in this repo starts here. The workflow is defined in `../CLAUDE.md` §2 and the
principles in `constitution.md`.

## Layout

```
specs/
  constitution.md        principles every spec must respect
  _template.md           copy this for a new spec
  000-baseline/          the as-is system, reverse-engineered from code (documentation, not change)
  010-foundations/       planned work, one folder per spec
    spec.md
  020-.../
```

Numbering: `000` is the baseline; planned specs use `010`, `020`, … so related follow-ups can slot in
between (`015`). A spec folder may hold supporting files (examples, diagrams, data contracts).

## Status lifecycle

| Status | Meaning | Who moves it |
|---|---|---|
| `DRAFT` | Being written; open questions remain | author |
| `APPROVED` | Reviewed against the constitution; ready to build | user (AML SME) |
| `IMPLEMENTED` | Code + tests merged; implementation notes filled in | author |
| `SUPERSEDED` | Replaced by a later spec (link it) | author |

Baseline specs (`000-*`) use `AS-IS` instead: they describe current behaviour, including behaviour we may
later want to change. Disagreements with the as-is logic become new specs, not edits to the baseline.

## Index

| Spec | Title | Status |
|---|---|---|
| 000-baseline | As-is system (rules, ML, scoring, evaluation) + SME open questions | AS-IS (draft, awaiting SME review) |
| 010-foundations | Package structure, tests, lint, CI, baseline lock | planned |
| 020-stable-ids-event-store | Deterministic IDs, persistent event store | planned |
| 030-incremental-cycles | Idempotent per-cycle processing | planned |
| 040-config-driven-rules | Rules as configuration with lifecycle | planned |
| 050-alert-lifecycle | Statuses, suppression, dispositions | planned |
| 060-ml-operations | Out-of-time training, registry, drift, ranker | planned |
| 070-data-controls | Schema contracts, completeness checks | planned |
| 080-governance-pack | Model documentation, tuning evidence | planned |
| 100-platform/01-product-requirements | Rule Engine platform: product requirements (batch Python detection + Spring Boot control plane) | APPROVED (decisions D-1..D-9) |
| 100-platform/02-functional-requirements | Rule Engine platform: exact functional behaviour (69 FR, 55 AC) | DRAFT |

Keep this index up to date when a spec is added or changes status.
