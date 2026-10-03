"""Novelty rule (FR-37/38): a detection needs at least one trigger transaction not used by an earlier detection."""


def classify(trigger_ids, prior_ids):
    trigger, prior = set(trigger_ids), set(prior_ids)
    if trigger and trigger <= prior:
        return "SUPPRESSED_NO_NEW_EVIDENCE"
    if trigger & prior:
        return "NEW_WITH_OVERLAP"
    return "NEW"
