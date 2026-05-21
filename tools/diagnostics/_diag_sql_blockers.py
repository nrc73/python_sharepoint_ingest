"""Quick diagnostic: check for SQL blocking, locks and log pressure on ingest_dev."""
from sharepoint_ingest.config import load_settings
from sharepoint_ingest.sql_client import SqlClient

s = load_settings(env_override='dev')
c = SqlClient(s.sql)

print("=== ACTIVE REQUESTS on ingest_dev ===")
rows = c.query_rows(
    "SELECT r.session_id, r.blocking_session_id, r.wait_type, r.wait_time, "
    "r.status, r.command, LEFT(st.text, 300) AS sql_text "
    "FROM sys.dm_exec_requests r "
    "CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) st "
    "WHERE r.database_id = DB_ID('ingest_dev')"
)
if rows:
    for r in rows:
        print(r)
else:
    print("  (none)")

print()
print("=== LOCKS on dbo.dest_transactions_large ===")
rows = c.query_rows(
    "SELECT request_session_id, resource_type, resource_description, "
    "request_mode, request_status "
    "FROM sys.dm_tran_locks "
    "WHERE resource_database_id = DB_ID('ingest_dev') "
    "AND resource_associated_entity_id = OBJECT_ID('dbo.dest_transactions_large')"
)
if rows:
    for r in rows:
        print(r)
else:
    print("  (none)")

print()
print("=== OPEN TRANSACTIONS ===")
rows = c.query_rows(
    "SELECT s.session_id, s.login_name, s.status, "
    "at.transaction_id, at.name AS tran_name, "
    "at.transaction_begin_time, at.transaction_type, at.transaction_state "
    "FROM sys.dm_tran_active_transactions at "
    "JOIN sys.dm_tran_session_transactions st ON at.transaction_id = st.transaction_id "
    "JOIN sys.dm_exec_sessions s ON st.session_id = s.session_id "
    "WHERE s.database_id = DB_ID('ingest_dev')"
)
if rows:
    for r in rows:
        print(r)
else:
    print("  (none)")

print()
print("=== LOG SPACE USAGE ===")
rows = c.query_rows("SELECT name, log_size_mb = log_size, log_used_mb = log_used, status FROM sys.databases WHERE name = 'ingest_dev'")
for r in rows:
    print(r)

print()
print("=== dest_transactions_large ROW COUNT ===")
rows = c.query_rows("SELECT COUNT(1) AS cnt FROM dbo.dest_transactions_large")
print("  rows:", rows[0]['cnt'])
