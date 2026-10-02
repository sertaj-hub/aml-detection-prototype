---
description: Review a spec for testability, ambiguity, AML gaps and constitution compliance
argument-hint: <spec folder, e.g. 010-foundations>
---

Review `specs/$ARGUMENTS/spec.md`. Do not edit code. Report findings, most important first:

1. **Constitution** — check each principle in `specs/constitution.md`. Flag any unstated exception.
2. **Testability** — every FR has at least one AC; every AC is concrete (numbers, not "large"/"many");
   fire cases have matching not-fire and boundary cases.
3. **Ambiguity** — terms that two engineers could implement differently (time windows inclusive or
   exclusive, calendar vs rolling month, which party a transaction belongs to, currency, rounding).
4. **AML gaps** — evasion just under the threshold, legitimate look-alikes that will false-positive,
   joint/secondary holder handling, cross-product effects, explainability for the investigator.
5. **Model risk** — is section 7 credible? Does the change alter the alerted population without saying so?
6. **Consistency** — conflicts with `CLAUDE.md` invariants, baseline specs or other specs.

End with a verdict: `ready for approval` or `needs changes`, and the list of open questions for the user.
Only the user moves a spec to `APPROVED`.
