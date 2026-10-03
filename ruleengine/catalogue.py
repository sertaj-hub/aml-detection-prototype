"""Governed field catalogue v0 (FR-11): the only fields a rule may reference, with type and SQL column.

Java analogy: an enum of allowed attributes, each with a type used for operator checking.
"""
FIELDS = {
    "transaction.amount": ("numeric", "amount"),
    "transaction.type": ("string", "txn_type"),
    "transaction.direction": ("string", "direction"),
    "transaction.channel": ("string", "channel"),
    "transaction.is_cash": ("boolean", "is_cash"),
    "transaction.product_type": ("string", "product_type"),
    "transaction.country": ("string", "cp_country"),
    "transaction.counterparty_type": ("string", "cp_type"),
    "transaction.counterparty_relation": ("string", "cp_relation"),     # derived: OWN | EXTERNAL
    "party.expected_monthly_credits": ("numeric", "exp_credits"),
    "party.expected_monthly_cash": ("numeric", "exp_cash"),
    "party.pep_flag": ("boolean", "pep_flag"),
    "party.kyc_risk": ("string", "kyc_risk"),
}

OPERATORS = {
    "numeric": {"EQUALS", "NOT_EQUALS", "IN", "NOT_IN", "LESS_THAN", "LESS_THAN_OR_EQUAL", "GREATER_THAN",
                "GREATER_THAN_OR_EQUAL", "BETWEEN", "IS_NULL", "IS_NOT_NULL"},
    "string": {"EQUALS", "NOT_EQUALS", "IN", "NOT_IN", "IS_NULL", "IS_NOT_NULL"},
    "boolean": {"EQUALS", "NOT_EQUALS", "IS_NULL", "IS_NOT_NULL"},
}
ALL_OPERATORS = sorted(set().union(*OPERATORS.values()))
COMPARISONS = {"EQUALS": "=", "NOT_EQUALS": "<>", "LESS_THAN": "<", "LESS_THAN_OR_EQUAL": "<=", "GREATER_THAN": ">",
               "GREATER_THAN_OR_EQUAL": ">="}

ENTITY_KEYS = {"PARTY": "party_id", "ACCOUNT": "account_id"}        # party = PRIMARY party of the account
SPLIT_FIELDS = {"direction": "direction", "type": "txn_type"}
SEVERITIES = ["HIGH", "MEDIUM", "LOW"]
AGG_FUNCTIONS = ["COUNT", "SUM", "MAX", "MIN"]
WINDOW_UNITS = {"HOURS": 3600 * 10**6, "DAYS": 86400 * 10**6}       # microseconds
EXPLANATION_EXTRAS = {"anchorAmount", "expected", "expectedCash"}
