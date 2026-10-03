# CLAUDE.md

Guidance for Claude Code (and human engineers) working in this repository.

## 1. What this is

A retail-bank **AML transaction monitoring** prototype: hybrid detection (15 deterministic rules + 3
unsupervised anomaly models) over **synthetic** data, rolled up into explainable party-level alerts and
measured against injected ground truth. See `README.md` for results and the data dictionary.

All data is synthetic (seed 42). Never introduce real customer data, real names of institutions, or
official watch-lists presented as real.

## 2. How we work: spec-driven development (mandatory)

**No behaviour change without an APPROVED spec.** The spec is the source of truth; code implements it.

1. **Spec** — create `specs/NNN-<name>/spec.md` from `specs/_template.md` (`/spec-new <name>`).
   Status starts as `DRAFT`. Ask the user clarifying questions rather than guessing AML intent.
2. **Review** — check the spec against `specs/constitution.md` and for testability (`/spec-review`).
   The user (AML SME) approves; only then set status to `APPROVED`.
3. **Tests first** — every acceptance criterion `AC-x` becomes at least one test that references it by ID
   (e.g. `test_ac3_structuring_needs_three_deposits`). Tests must fail before implementation.
4. **Implement** — the smallest change that makes the tests pass (`/spec-implement`).
5. **Verify** — run tests + lint; run the pipeline when detection, scoring or evaluation changes and compare
   with the baseline metrics (§6). Record any intended metric change and its reason in the spec.
6. **Close** — set status to `IMPLEMENTED`, fill in the spec's *Implementation notes*.

Exempt from a spec: typo fixes, comments, formatting, dependency pins, and docs that do not change behaviour.
If a spec turns out to be wrong mid-implementation, stop, update the spec, and get re-approval.

Spec numbering: `000` baseline (as-is system), then `010`, `020`, … for planned work, leaving gaps for
inserts. See `specs/README.md`.

## 3. Architecture map

Pipeline (`run_pipeline.py`): data → rules → ML events → scoring → alerts → evaluation.

| File | Responsibility | Java mental model |
|---|---|---|
| `config.py` | All parameters, thresholds, scoring weights | `final class AmlConfig` constants |
| `common.py` | ID generators, columnar txn buffer, date helpers | utility classes |
| `generate_data.py` | Synthetic parties, accounts, roles, normal behaviour | data factory |
| `typologies.py` | 18 laundering typologies + legitimate look-alikes | strategy per typology |
| `rules_engine.py` | 15 rules → `detection_events` + `event_transactions` | `interface Rule { List<Event> evaluate(ctx) }` |
| `ml_events.py` | Per-product IsolationForest + SHAP reason codes → ML events | feature builder + model wrapper |
| `scoring_alerts.py` | Repeat discount, roll-up, bonuses, alert tables | scoring service |
| `evaluate.py` | Precision / recall vs ground truth, threshold sweep | test harness |
| `show_alert.py` | Investigator drill-down tree | CLI view |

Score hierarchy: transaction contribution → event effective points → account score → party alert score.
Alerts are raised on the **primary party** per monthly cycle.

## 4. Commands

```bash
pip install -r requirements.txt
python3 run_pipeline.py            # regenerate data + full detection (~3 min)
python3 run_pipeline.py --no-data  # rerun detection on existing data/ (~2 min)
python3 show_alert.py --top 3      # inspect alerts
```

Rule Engine MVP (spec `110-mvp`, package `ruleengine/`):

```bash
python3 -m pytest -q                                   # 53 tests, names follow the acceptance criteria (MAC-n)
python3 -m ruleengine validate ruleengine/rules/*.json  # validate rule files
python3 -m ruleengine run --date 2026-06-15 --dry-run   # compute, print, write nothing
python3 -m ruleengine run --from 2026-04-01 --to 2026-09-30   # persists to out/engine.db
python3 -m ruleengine publish                           # outbox stub -> out/alerts.jsonl
PYTHONPATH=. python3 scripts/mvp_conformance.py         # compare with the prototype and labels (~90 s)
```

Prototype tests and lint are still introduced by spec `010-foundations`.

## 5. Invariants — never break these

1. **Label isolation**: `labels_*`, `schemes`, `legit_spikes` are read only by `evaluate.py` (and tests).
   Detection, ML and scoring code must never read them.
2. **Reconciliation**: transaction contributions sum to event effective points, events to account scores,
   accounts (+ bonuses) to the alert score, each within 0.05 points.
3. **Drill-down**: every alert reaches at least one TRIGGER transaction through `event_transactions`.
4. **Determinism**: same seed + same code ⇒ identical outputs.
5. **Explainability**: every event carries a plain-English `observed` vs `threshold` (rules) or reason codes (ML).
6. **No silent volume change**: any change to alert volume, productive rate or recall must be stated in the
   spec with before/after numbers.

## 6. Baseline metrics (Rules + ML, threshold 35)

321 alerts over 6 months · productive rate 42% · party recall 78% · scheme recall 87% (62 of 71).
Source: `data/evaluation_threshold_sweep.parquet` (CSV copy in `exports/`). A change that moves these must say so in its spec.

## 7. Conventions

- Rule IDs `R-<DEP|CRD|LN|XP>-NN`; model IDs `M-ANOM-<DEP|CRD|LN>`. A new rule needs: catalog row in
  `RULES`, implementation, spec entry, tests for fire / not-fire / boundary.
- Thresholds and weights live in `config.py` (or rule config), never as literals inside logic.
- Match existing style: module docstring explaining purpose + a Java analogy where it helps; concise comments.
- pandas vectorised operations over row loops where practical.
- Commits reference the spec: `[spec 010] add reconciliation test`.

## 8. Definition of done

- [ ] Spec `APPROVED` before coding, `IMPLEMENTED` after.
- [ ] Every acceptance criterion has a passing test that names it.
- [ ] Invariants in §5 still hold.
- [ ] Baseline metrics unchanged, or the change is documented in the spec.
- [ ] README / CLAUDE.md updated if commands, files or behaviour changed.
