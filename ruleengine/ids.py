"""Deterministic identifiers (FR-39, FR-43)."""
import hashlib


def _h(*parts, n=16):
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:n]


def detection_id(rule_id, entity_key, split_key, trigger_txn_ids, window_end):
    window_end = window_end.isoformat() if hasattr(window_end, "isoformat") else str(window_end)
    return "DET-" + _h(rule_id, entity_key, split_key, ",".join(sorted(trigger_txn_ids)), window_end)


def alert_key(primary_party_id, cycle_type, cycle_date, policy_version):
    return "AK-" + _h(primary_party_id, cycle_type, cycle_date, policy_version)
