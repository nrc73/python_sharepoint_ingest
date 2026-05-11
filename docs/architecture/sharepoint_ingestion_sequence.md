# Sequence Diagram – Python, AKV, SharePoint, and SQL Calls

This sequence focuses on connection/auth call flow and data movement between:

- Python pre-check / ingestion runner
- Azure Key Vault (AKV)
- SharePoint Online
- SQL Server database

## 1) Pre-check sequence (dev/prod or all)

```mermaid
sequenceDiagram
    autonumber
    actor Ops as Operator / Scheduler
    participant Py as Python pre-check scripts
    participant AKV as Azure Key Vault
    participant SP as SharePoint Online
    participant SQL as SQL Server (ingest_dev / ingest_prod)

    Note over Py: Run with --env dev, --env prod, or --env all

    Ops->>Py: Execute pre-check scripts

    Py->>AKV: HTTPS 443 - Retrieve secret set (per env)
    AKV-->>Py: SP client_id/client_secret/tenant_id, SQL credentials/settings

    Py->>SP: HTTPS 443 - Authenticate app + test folder listing
    SP-->>Py: Auth result + file count / error

    Py->>SQL: TCP 1433 (TDS) - Open connection + SELECT 1
    SQL-->>Py: Connection/login result

    Py-->>Ops: Consolidated pass/fail by environment
```

## 2) Ingestion runtime sequence

```mermaid
sequenceDiagram
    autonumber
    actor Ops as Operator / Scheduler
    participant Py as Python runner (src.main)
    participant AKV as Azure Key Vault
    participant SP as SharePoint Online
    participant SQL as SQL Server

    Ops->>Py: Start ingestion run (--env dev|prod)

    Py->>AKV: HTTPS 443 - Resolve runtime secrets/config
    AKV-->>Py: Environment credential set

    Py->>SQL: TCP 1433 - Read config.sharepoint_ingestion
    SQL-->>Py: Active ingestion configs

    loop per configured SharePoint folder / file pattern
        Py->>SP: HTTPS 443 - List files
        SP-->>Py: File metadata list

        loop each matching file
            Py->>SP: HTTPS 443 - Download file payload
            SP-->>Py: CSV/Excel bytes

            Py->>SQL: TCP 1433 - Load/append/merge into target table
            SQL-->>Py: DML outcome

            Py->>SQL: TCP 1433 - Insert audit row to log.sharepoint_ingestion_audit
            SQL-->>Py: Audit insert result

            alt Success
                Py->>SP: HTTPS 443 - Move file to archive folder
                SP-->>Py: Move success
            else Failure
                Py->>SP: HTTPS 443 - Move file to failed folder
                SP-->>Py: Move success/failure
            end
        end
    end

    Py-->>Ops: Summary + exit code
```

## Port call matrix

| Source | Destination | Protocol | Port | Call Type |
|---|---|---:|---:|---|
| Python | Azure Key Vault | HTTPS | 443 | Secret retrieval |
| Python | SharePoint Online | HTTPS | 443 | Auth, list, download, move |
| Python | SQL Server | TDS/TCP | 1433 | Config reads, data writes, audit writes |
