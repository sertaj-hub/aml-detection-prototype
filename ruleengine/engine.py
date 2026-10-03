"""Daily run orchestration (FR-M5..M12): rules -> detections -> novelty -> party-level alerts -> persistence."""
import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ruleengine import __version__, store
from ruleengine.evaluate import SEVERITY_ORDER, evaluate_rule
from ruleengine.ids import alert_key
from ruleengine.novelty import classify
from ruleengine.policy import points_for, policy_sha


@dataclass
class RunResult:
    run_id: str
    status: str
    business_date: date
    detections: list = field(default_factory=list)
    alerts: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)


class Engine:
    def __init__(self, data, rules, policy, store_conn):
        self.data, self.rules, self.policy, self.store = data, rules, policy, store_conn

    # ------------------------------------------------------------------ manifest
    def manifest(self, business_date):
        total, amount, last = self.data.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount), 0), MAX(txn_ts) FROM transactions WHERE txn_ts < ?",
            [datetime.combine(business_date + timedelta(days=1), datetime.min.time())]).fetchone()
        active = [r for r in self.rules if r.status == "ACTIVE" and r.cadence == "DAILY"]
        return {"businessDate": business_date.isoformat(), "engineVersion": __version__,
                "rules": {r.rule_id: {"version": r.version, "sha256": r.sha256} for r in active},
                "policy": {"id": self.policy["policyId"], "version": self.policy["version"], "sha256": policy_sha(self.policy)},
                "inputFingerprint": {"transactionsUpToDate": total, "amountSum": float(amount), "lastTxn": str(last)}}

    # ------------------------------------------------------------------ run
    def run_date(self, business_date, dry_run=False):
        s = self.store
        run_id = "RUN-" + uuid.uuid4().hex[:12]
        manifest = self.manifest(business_date)
        res = RunResult(run_id=run_id, status="RUNNING", business_date=business_date)
        started = datetime.utcnow().isoformat()
        s.execute("BEGIN")
        try:
            s.execute("INSERT INTO run VALUES (?,?,?,?,?,?,NULL,?,NULL,NULL)",
                      [run_id, "DAILY", business_date.isoformat(), "DRY_RUN" if dry_run else "PRODUCTION", "RUNNING", started,
                       json.dumps(manifest, sort_keys=True)])
            failures, successes = self._detect(res, run_id, business_date)
            self._alert(res, run_id, business_date)
            res.status = "SUCCEEDED" if not failures else ("FAILED" if successes == 0 and failures == len(manifest["rules"]) and
                                                           all(w.startswith("EXEC:") for w in res.warnings) else "PARTIAL")
            if res.warnings and res.status == "SUCCEEDED":
                res.status = "PARTIAL"
            res.counts = {"detections": len(res.detections),
                          "suppressed": sum(d["status"] == "SUPPRESSED_NO_NEW_EVIDENCE" for d in res.detections),
                          "alerts": len(res.alerts), "ruleFailures": failures}
            s.execute("UPDATE run SET status=?, finished_at=?, counts_json=?, warnings_json=? WHERE run_id=?",
                      [res.status, datetime.utcnow().isoformat(), json.dumps(res.counts), json.dumps(res.warnings), run_id])
            s.execute("ROLLBACK" if dry_run else "COMMIT")
        except BaseException:
            s.execute("ROLLBACK")
            raise
        return res

    def _detect(self, res, run_id, business_date):
        s, failures, successes = self.store, 0, 0
        for rule in self.rules:
            if rule.status != "ACTIVE" or rule.cadence != "DAILY":
                continue
            t0 = datetime.utcnow().isoformat()
            status, error, kept = "SUCCEEDED", None, []
            try:
                dets = evaluate_rule(self.data, rule, business_date)
                bad = [d for d in dets if not d["reconciled"]]
                if bad:
                    status, error = "RECONCILIATION_FAILED", "; ".join(e for d in bad for e in d["reconciliation_errors"])
                    res.warnings.append(f"RECON:{rule.rule_id}: {error}")
                    failures += 1
                else:
                    successes += 1
                kept = [d for d in dets if d["reconciled"]]
            except Exception as e:                                  # rule isolation: record and continue (FR-23)
                status, error = "FAILED", f"{type(e).__name__}: {e}"
                res.warnings.append(f"EXEC:{rule.rule_id}: {error}")
                failures += 1
            for d in kept:
                prior = store.prior_trigger_ids(s, d["rule_id"], d["entity_key"], d["split_key"], business_date)
                d["status"] = classify(d["trigger_txn_ids"], prior)
                d["points"] = points_for(self.policy, d["severity"], d["ratio"])
                d["run_id"] = run_id
                store.record_detection(s, d)
                res.detections.append(d)
            s.execute("INSERT INTO rule_execution VALUES (?,?,?,?,?,?,?,?)",
                      [run_id, rule.rule_id, rule.version, status, error, len(kept), t0, datetime.utcnow().isoformat()])
        return failures, successes

    # ------------------------------------------------------------------ alerts
    def _alert(self, res, run_id, business_date):
        s, pol = self.store, self.policy
        rows = s.execute(
            "SELECT detection_id, rule_id, rule_version, party_id, severity, points, ratio, status, metrics_json, evidence_json, "
            "window_end FROM detection WHERE business_date=? AND status IN ('NEW','NEW_WITH_OVERLAP') ORDER BY party_id, detection_id",
            [business_date.isoformat()]).fetchall()
        by_party = {}
        for r in rows:
            by_party.setdefault(r[3], []).append(r)
        for party, dets in sorted(by_party.items()):
            score = round(sum(r[5] for r in dets), 1)
            if score < pol["dailyThreshold"]:
                continue
            key = alert_key(party, "DAILY", business_date.isoformat(), pol["version"])
            if s.execute("SELECT 1 FROM alert WHERE alert_key=?", [key]).fetchone():
                continue                                    # idempotent re-run (FR-M11)
            alert = self._build_alert(run_id, business_date, party, key, score, dets)
            try:
                store.create_alert(s, alert)
            except Exception as e:
                res.warnings.append(f"ALERT:{party}: {type(e).__name__}: {e}")
                continue
            res.alerts.append(alert)

    def _build_alert(self, run_id, business_date, party, key, score, dets):
        pol = self.policy
        detections, accounts, initiators = [], set(), set()
        for (did, rid, rver, _, sev, pts, ratio, status, metrics, evidence, wend) in dets:
            ev = json.loads(evidence)
            for t in ev["trigger_transactions"]:
                accounts.add(t["account_id"])
                if t["initiated_by_party_id"]:
                    initiators.add(t["initiated_by_party_id"])
            detections.append({"detectionId": did, "ruleId": rid, "ruleVersion": rver, "ruleName": ev["rule_name"],
                               "regulation": ev["regulation"], "severity": sev, "points": pts, "ratio": ratio,
                               "status": status, "windowEnd": wend, "metrics": json.loads(metrics),
                               "explanation": ev["explanation"], "triggerTransactions": ev["trigger_transactions"]})
        sec = {}
        if accounts:
            q = ",".join("?" * len(accounts))
            for pid, role in self.data.execute(
                    f"SELECT party_id, role FROM party_account_role WHERE role <> 'PRIMARY' AND account_id IN ({q}) ORDER BY party_id, role",
                    sorted(accounts)).fetchall():
                sec.setdefault(pid, role)
        parties = [(party, "PRIMARY", party in initiators)] + [(p, r, p in initiators) for p, r in sorted(sec.items()) if p != party]
        severity = max((d["severity"] for d in detections), key=lambda x: SEVERITY_ORDER[x])
        seq = store.next_alert_seq(self.store, business_date.isoformat()) + 0
        alert_id = f"ALT-{business_date:%Y%m%d}-{seq:05d}"
        rule_versions = {d["ruleId"]: d["ruleVersion"] for d in detections}
        return {
            "alert_id": alert_id, "alert_key": key, "primary_party_id": party, "cycle_type": "DAILY",
            "cycle_date": business_date.isoformat(), "policy_version": pol["version"], "score": score,
            "threshold": pol["dailyThreshold"], "severity": severity, "run_id": run_id,
            "detection_ids": [d["detectionId"] for d in detections], "parties": parties,
            "evidence": {"alertId": alert_id, "primaryPartyId": party, "cycleType": "DAILY", "cycleDate": business_date.isoformat(),
                         "score": score, "threshold": pol["dailyThreshold"], "policyVersion": pol["version"],
                         "relatedParties": [{"partyId": p, "role": r, "isInitiator": bool(i)} for p, r, i in parties],
                         "detections": detections},
            "event": {"alertId": alert_id, "alertKey": key, "primaryPartyId": party, "cycleType": "DAILY",
                      "cycleDate": business_date.isoformat(), "severity": severity, "score": score,
                      "ruleIds": sorted(rule_versions), "ruleVersions": rule_versions, "detectionCount": len(detections)},
            "detection_status": [d["status"] for d in detections],
        }
