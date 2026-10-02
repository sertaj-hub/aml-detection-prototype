"""
common.py - Shared helpers: ID generators, a column-oriented transaction buffer, date helpers.

Java analogy:
  - TxnBuffer is like a builder that holds one ArrayList per column (struct-of-arrays) instead
    of an ArrayList<Transaction> (array-of-structs). Columnar lists convert to a pandas
    DataFrame far faster and use much less memory than millions of dicts/objects.
"""
from datetime import datetime, timedelta
import calendar
import pandas as pd

TXN_COLUMNS = [
    "txn_id", "account_id", "product_type", "txn_ts", "txn_type", "direction", "amount",
    "channel", "is_cash", "counterparty_id", "counter_account_id", "cp_country", "mcc",
    "location_state", "initiated_by_party_id", "description",
]


class IdGen:
    """Sequential, prefixed IDs. Java: an AtomicLong counter with String.format."""

    def __init__(self, prefix: str, width: int, start: int = 1):
        self.prefix, self.width, self.n = prefix, width, start - 1

    def next(self) -> str:
        self.n += 1
        return f"{self.prefix}{self.n:0{self.width}d}"   # Java: String.format("%s%07d", ...)


class TxnBuffer:
    """Column-oriented buffer for transactions (struct-of-arrays)."""

    def __init__(self):
        # dict comprehension - Java: Map<String, List<Object>> cols = new HashMap<>(); loop put(...)
        self.cols = {c: [] for c in TXN_COLUMNS}
        self.ids = IdGen("T", 9)

    def add(self, account_id, product_type, txn_ts, txn_type, direction, amount, channel,
            is_cash=False, counterparty_id=None, counter_account_id=None, cp_country="US",
            mcc=None, location_state=None, initiated_by_party_id=None, description=""):
        if amount < 0.01:          # guard: never emit zero / negative amounts
            return None
        txn_id = self.ids.next()
        row = (txn_id, account_id, product_type, txn_ts, txn_type, direction, round(float(amount), 2),
               channel, is_cash, counterparty_id, counter_account_id, cp_country, mcc,
               location_state, initiated_by_party_id, description)
        # zip pairs each column list with its value - Java: for (int i...) cols.get(name[i]).add(row[i])
        for col, val in zip(TXN_COLUMNS, row):
            self.cols[col].append(val)
        return txn_id

    def to_frame(self) -> pd.DataFrame:
        df = pd.DataFrame(self.cols)
        return df.sort_values(["txn_ts", "txn_id"]).reset_index(drop=True)


def month_bounds(month: str):
    """'2026-04' -> (datetime(2026,4,1), number_of_days)."""
    y, m = map(int, month.split("-"))
    return datetime(y, m, 1), calendar.monthrange(y, m)[1]


def ts_on(day: datetime, rng, hour_lo=8, hour_hi=20) -> datetime:
    """Random timestamp on a given day within business-ish hours."""
    return day.replace(hour=0, minute=0, second=0) + timedelta(
        hours=int(rng.integers(hour_lo, hour_hi)), minutes=int(rng.integers(0, 60)),
        seconds=int(rng.integers(0, 60)))


def rand_day(month_start: datetime, n_days: int, rng, lo=1, hi=None) -> datetime:
    """Random day within a month (1-based lo..hi inclusive)."""
    hi = hi or n_days
    return month_start + timedelta(days=int(rng.integers(lo, hi + 1)) - 1)


def amortized_payment(principal: float, annual_rate: float, months: int) -> float:
    r = annual_rate / 12
    return principal * r / (1 - (1 + r) ** -months)
