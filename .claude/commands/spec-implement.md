---
description: Implement an APPROVED spec test-first and verify it
argument-hint: <spec folder, e.g. 010-foundations>
---

Implement `specs/$ARGUMENTS/spec.md`.

1. Read the spec, `CLAUDE.md` and `specs/constitution.md`. If the status is not `APPROVED`, stop and tell the
   user. If anything in the spec is ambiguous, stop and ask; do not resolve it silently.
2. Propose a short implementation plan (files to change, tests to add, AC → test mapping) and wait for the
   user's go-ahead.
3. **Tests first**: write one or more tests per acceptance criterion, named after it
   (`test_ac<N>_<what>`). Run them and confirm they fail for the right reason.
4. Implement the smallest change that makes them pass. Respect the invariants in `CLAUDE.md` §5.
5. Verify: run the full test suite and lint. If detection, scoring or evaluation changed, run the pipeline
   and compare metrics with the baseline in `CLAUDE.md` §6.
6. Fill in section 10 (*Implementation notes*) of the spec: files changed, tests added, actual metric
   changes, deviations. Set status to `IMPLEMENTED` and update `specs/README.md`.
7. Commit with the spec prefix, e.g. `[spec 010] ...`. Report results to the user, including any failing check.
