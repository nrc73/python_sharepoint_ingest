"""Generate a ~500 MB parquet file with globally unique transaction_ids.

Writes to tests/sample_artifacts/valid_transactions_parquet_500mb.parquet
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

CATEGORIES = ["Electronics", "Clothing", "Food", "Books", "Toys", "Sports", "Home", "Health"]
STATUSES = ["COMPLETED", "PENDING", "CANCELLED", "REFUNDED"]
TARGET_MB = 490.0  # write until on-disk size exceeds this

def random_date(start: date, end: date) -> date:
    delta = (end - start).days
    return start + timedelta(days=random.randint(0, delta))

def generate_batch(start_id: int, n: int) -> pa.Table:
    ids = [f"TXN{str(start_id + i).zfill(10)}" for i in range(n)]
    df = pd.DataFrame({
        "transaction_id":   ids,
        "customer_id":      [f"CUST{str(random.randint(1, 500_000)).zfill(7)}" for _ in range(n)],
        "transaction_date": [str(random_date(date(2019, 1, 1), date(2025, 12, 31))) for _ in range(n)],
        "amount":           [round(random.uniform(0.01, 99_999.99), 2) for _ in range(n)],
        "category":         [random.choice(CATEGORIES) for _ in range(n)],
        "status":           [random.choice(STATUSES) for _ in range(n)],
        "description":      ["".join(random.choices(string.ascii_letters + " ", k=60)) for _ in range(n)],
        "notes":            ["".join(random.choices(string.ascii_letters + " ", k=80)) for _ in range(n)],
        "region":           [random.choice(["AU", "NZ", "US", "UK", "SG", "JP"]) for _ in range(n)],
        "source_file_name": [None] * n,
    })
    return pa.Table.from_pandas(df, preserve_index=False)

OUTPUT = os.path.join("tests", "sample_artifacts", "valid_transactions_parquet_500mb.parquet")
os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)

ROWS_PER_BATCH = 50_000
global_id = 0
batches_written = 0

# Write first batch to get the schema
first_batch = generate_batch(0, ROWS_PER_BATCH)
schema = first_batch.schema
global_id += ROWS_PER_BATCH
batches_written += 1

print(f"Generating ~500 MB Parquet → {OUTPUT}")
print(f"  Schema: {', '.join(schema.names)}")

with pq.ParquetWriter(OUTPUT, schema) as writer:
    writer.write_table(first_batch)
    size_mb = os.path.getsize(OUTPUT) / (1024 * 1024)
    print(f"  Batch {batches_written:4d}:  {global_id:>12,} rows  {size_mb:6.1f} MB", flush=True)

    while size_mb < TARGET_MB:
        batch = generate_batch(global_id, ROWS_PER_BATCH)
        writer.write_table(batch)
        global_id += ROWS_PER_BATCH
        batches_written += 1
        size_mb = os.path.getsize(OUTPUT) / (1024 * 1024)
        if batches_written % 5 == 0:
            print(f"  Batch {batches_written:4d}:  {global_id:>12,} rows  {size_mb:6.1f} MB", flush=True)

final_mb = os.path.getsize(OUTPUT) / (1024 * 1024)
print(f"\nDone.")
print(f"  Output : {OUTPUT}")
print(f"  Rows   : {global_id:,}")
print(f"  Size   : {final_mb:.2f} MB")
print(f"  Batches: {batches_written}")
