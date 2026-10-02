---
description: Scaffold a new spec from the template and draft it with the user
argument-hint: <NNN-short-name> [one-line intent]
---

Create a new spec for: $ARGUMENTS

1. Read `CLAUDE.md`, `specs/README.md`, `specs/constitution.md` and `specs/_template.md`.
2. If no number was given, pick the next free number in `specs/README.md` (gaps of 10; use 5s for inserts).
   Create `specs/<NNN-short-name>/spec.md` from the template with status `DRAFT` and today's date.
3. Read the code the change touches (and any `specs/000-baseline/` spec covering it) so the draft is grounded
   in current behaviour. Quote current thresholds and behaviour from the code, not from memory.
4. Draft sections 1–8. Requirements must be testable; every fire case gets a not-fire and a boundary
   acceptance criterion. Fill section 7 with expected metric impact, or "none" and how that is verified.
5. Put anything that depends on AML judgement (thresholds, typology intent, alert routing) in
   section 9 as open questions. Do not guess them.
6. Add the spec to the index in `specs/README.md`.
7. Do not write or change any code. Summarise the draft and list the open questions for the user.
