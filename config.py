"""
config.py - Central configuration for the AML synthetic data + detection pipeline.

Java analogy: think of this module as a `final class AmlConfig { public static final ... }`.
In Python, module-level UPPER_CASE names are the convention for constants.
"""
from datetime import datetime, date

# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------
SEED = 42  # Java: new Random(42) - same seed => same dataset every run

# ---------------------------------------------------------------------------
# Monitoring window: 6 months
# ---------------------------------------------------------------------------
WINDOW_START = datetime(2026, 4, 1)
WINDOW_END = datetime(2026, 9, 30, 23, 59, 59)
MONTHS = ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]

# ---------------------------------------------------------------------------
# Population
# ---------------------------------------------------------------------------
N_PRIMARY_PARTIES = 5000          # parties who are primary on at least one account
SECONDARY_ONLY_SHARE = 0.12       # extra parties who only appear as joint/AU/co-borrower

SEGMENTS = {
    # segment: (share, annual income range)
    "MASS":           (0.45, (28_000, 70_000)),
    "MASS_AFFLUENT":  (0.25, (70_000, 150_000)),
    "AFFLUENT":       (0.08, (150_000, 450_000)),
    "STUDENT":        (0.08, (4_000, 22_000)),
    "RETIREE":        (0.09, (22_000, 80_000)),
    "SELF_EMPLOYED":  (0.05, (40_000, 180_000)),
}

OCCUPATIONS = {
    "MASS": ["Retail Associate", "Warehouse Worker", "Nurse Aide", "Teacher", "Driver",
             "Administrative Assistant", "Electrician", "Customer Service Rep", "Cook"],
    "MASS_AFFLUENT": ["Software Engineer", "Registered Nurse", "Accountant", "Project Manager",
                      "Police Officer", "Pharmacist", "Sales Manager", "Engineer"],
    "AFFLUENT": ["Physician", "Attorney", "Senior Executive", "Business Owner", "Dentist",
                 "Investment Banker"],
    "STUDENT": ["Student"],
    "RETIREE": ["Retired"],
    "SELF_EMPLOYED": ["Restaurant Owner", "Hair Salon Owner", "Contractor", "Food Truck Owner",
                      "Consultant", "Convenience Store Owner", "Auto Repair Shop Owner"],
}
CASH_INTENSIVE_OCCUPATIONS = {"Restaurant Owner", "Hair Salon Owner", "Food Truck Owner",
                              "Convenience Store Owner", "Auto Repair Shop Owner"}

# Product holding (share of primary parties)
P_DEPOSIT = 0.75
P_SAVINGS_GIVEN_DEPOSIT = 0.50
P_CD_GIVEN_DEPOSIT = 0.10
P_CARD = 0.55
P_LOAN = 0.30
LOAN_MIX = {"PERSONAL_LOAN": 0.40, "AUTO_LOAN": 0.40, "MORTGAGE": 0.20}
P_SECONDARY_HOLDER = 0.20         # share of accounts with a joint / AU / co-borrower

# Share of parties with a legitimate one-off spike (false-positive fodder)
P_LEGIT_SPIKE = 0.07

# ---------------------------------------------------------------------------
# Reference data (illustrative only - NOT an official or current list)
# ---------------------------------------------------------------------------
HIGH_RISK_COUNTRIES = ["IR", "KP", "MM", "SY", "YE", "HT", "VE", "SS"]
LEGIT_REMITTANCE_COUNTRIES = ["MX", "IN", "PH", "GT", "SV", "DO", "VN", "NG", "PK", "CO"]
US_STATES = ["NY", "NJ", "CA", "TX", "FL", "IL", "PA", "GA", "NC", "MA", "VA", "WA", "AZ", "OH", "MI"]

MCC = {
    # mcc: (description, mean amount, sigma for lognormal)
    "5411": ("Grocery", 75, 0.6),
    "5812": ("Restaurants", 38, 0.6),
    "5541": ("Gas Station", 45, 0.4),
    "5999": ("Online Retail", 60, 0.9),
    "5311": ("Department Store", 85, 0.7),
    "4900": ("Utilities", 120, 0.4),
    "4511": ("Airlines", 420, 0.5),
    "7011": ("Hotels", 260, 0.6),
    "5912": ("Pharmacy", 30, 0.6),
    "7832": ("Entertainment", 25, 0.5),
    "5732": ("Electronics", 240, 0.8),
    "7995": ("Gambling", 300, 0.7),   # high-risk MCC
    "6051": ("Crypto / Quasi-cash", 500, 0.8),  # high-risk MCC
}
HIGH_RISK_MCC = {"7995", "6051"}
NORMAL_MCC_WEIGHTS = {"5411": 0.25, "5812": 0.20, "5541": 0.12, "5999": 0.15, "5311": 0.07,
                      "4900": 0.04, "4511": 0.01, "7011": 0.01, "5912": 0.06, "7832": 0.06,
                      "5732": 0.02, "7995": 0.005, "6051": 0.005}

# ---------------------------------------------------------------------------
# Scoring / alerting
# ---------------------------------------------------------------------------
ALERT_THRESHOLD = 35          # chosen from the threshold sweep (see README) - one strong scenario alone can alert
NEAR_MISS_BAND = 15           # parties within this band below threshold are kept for BTL testing
RULE_REPEAT_FACTOR = 0.25     # repeated hits of the same rule add 25% of their points...
RULE_REPEAT_CAP = 2.0         # ...capped at 2x the rule's base points
CROSS_PRODUCT_BONUS = {2: 10, 3: 15}   # events spanning N product types
KYC_HIGH_RISK_BONUS = 5

ML_EVENT_PERCENTILE = 99.0    # account-months above this anomaly percentile raise an ML event
ML_POINTS_MIN, ML_POINTS_MAX = 10, 25
