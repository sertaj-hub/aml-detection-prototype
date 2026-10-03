"""Data access: DuckDB views over the prototype's Parquet files (stand-in for Databricks/Delta, D-2)."""
from pathlib import Path

import duckdb

TABLES = ("transactions", "party_account_role", "parties", "counterparties")


def open_parquet(data_dir):
    conn = duckdb.connect()
    for t in TABLES:
        f = Path(data_dir) / f"{t}.parquet"
        conn.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{f.as_posix()}')")
    return conn
