# Constitution

Non-negotiable principles. Every spec must state how it respects them; a spec that needs to break one
must say so explicitly and be approved as an exception.

## 1. Explainability before accuracy
An alert an investigator cannot explain is not usable. Every detection must expose what was observed,
against which threshold or reason, and which transactions drove it. A more accurate but opaque change is
rejected unless it ships with equivalent explanation.

## 2. Reproducibility
The same inputs, configuration and code produce the same outputs. Randomness is seeded. Every output can be
traced to the code and configuration version that produced it.

## 3. Ground truth stays out of detection
Labels exist only to measure the system. Detection, feature engineering, model training for anomaly events,
scoring and alerting never read them. (A future supervised model may train on labels or investigator
dispositions, but only through an explicit, time-split, spec-approved pipeline.)

## 4. No silent change to the alerted population
Any change that alters alert volume, productive rate or recall is a model change. Its spec records
before/after metrics and the reason, as a model validator would require.

## 5. Honest evaluation
Prefer out-of-time validation over in-sample results. State caveats (synthetic data, threshold tuned on the
same data) next to any number that is quoted.

## 6. Regulator-readable
Specs, rule logic and model descriptions are written so a compliance officer or model validator can follow
them without reading code.

## 7. Synthetic data only
No real customer data, no real institutions impersonated, and illustrative reference lists are labelled as
illustrative.

## 8. Simplicity
Build the smallest thing that satisfies the spec. Production-style patterns are introduced when a spec needs
them, not speculatively.
