"""Alert policy (MVP): points per severity and the party-level alert threshold (FR-41, D-4/D-5)."""
import hashlib
import json
from pathlib import Path


def load_policy(path):
    p = json.loads(Path(path).read_text())
    for k in ("policyId", "version", "severityPoints", "severityFactor", "dailyThreshold"):
        if k not in p:
            raise ValueError(f"policy missing '{k}'")
    return p


def policy_sha(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()


def points_for(policy, severity, ratio):
    f = policy["severityFactor"]
    factor = min(f["cap"], max(1.0, 1 + f["perRatioUnit"] * (ratio - 1)))
    return round(policy["severityPoints"][severity] * factor, 1)
