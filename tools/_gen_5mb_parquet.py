"""Generate a ~5 MB parquet file with globally unique transaction_ids.

Writes to tests/sample_artifacts/valid_transactions_parquet_5mb.parquet
"""
import os
import sys
sys.path.insert(0, ".")

import random
import string
from datetime import date, timedelta

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

STATUSES = ["COMPLETED", "PENDING", "CANCELLED"]
CURRENCIES = ["AUD", "USD", "GBP", "EUR", "NZD"]
SOURCE_SYSTEMS = ["CRM", "ERP", "POS", "WEB", "MOBILE"]

def random_date(start: date, end: date) -> date:
    delta = (end - start).days
    return start + timedelta(days=random.randint(0, delta))

def generate_batch(start_id: int, n: int) -> pd.DataFrame:
    ids = [f"TXN{str(start_id + i).zfill(9)}" for i in range(n)]
    return pd.DataFrame({
        "transaction_id":   ids,
        "customer_id":      [f"CUST{str(random.randint(1, 50000)).zfill(6)}" for _ in range(n)],
        "transaction_date": [str(random_date(date(2020, 1, 1), date(2025, 12, 31))) for _ in range(n)],
        "amount":           [round(random.uniform(1.0, 9999.99), 2) for _ in range(n)],
        "status":           [random.choice(STATUSES) for _ in range(n)],
        "currency":         [random.choice(CURRENCIES) for _ in range(n)],
        "source_system":    [random.choice(SOURCE_SYSTEMS) for _ in range(n)],
        "notes":            ["".join(random.choices(string.ascii_letters + " ", k=40)) for _ in range(n)],
        "source_file_name": [None] * n,
    })

OUTPUT = os.path.join("tests", "sample_artifacts", "valid_transactions_parquet_5mb.parquet")
os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)

ROWS_PER_BATCH = 20_000
batches_written = 0
global_id = 0
schema = None
writer = None

print("Generating 5 MB parquet…")
with pq.ParquetWriter(OUTPUT, schema=pa.Schema.from_pandas(generate_batch(0, 1))) as writer:
    while True:
        batch = generate_batch(global_id, ROWS_PER_BATCH)
        table = pa.Table.from_pandas(batch, preserve_index=False)
        if schema is None:
            schema = table.schema
        writer.write_table(table)
        global_id += ROWS_PER_BATCH
        batches_written += 1
        size_mb = os.path.getsize(OUTPUT) / (1024 * 1024)
        print(f"  Batch {batches_written}: {global_id:,} rows — {size_mb:.2f} MB on disk")
        if size_mb >= 4.8:
            break

final_mb = os.path.getsize(OUTPUT) / (1024 * 1024)
print(f"\nDone. {OUTPUT}")
print(f"  Rows: {global_id:,}  Size: {final_mb:.2f} MB  Unique IDs: {global_id:,}")
