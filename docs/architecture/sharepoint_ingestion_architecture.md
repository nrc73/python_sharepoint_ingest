# SharePoint Ingestion Architecture (Dev + Prod)

This document provides a Visio-style architecture view for both environments and can be rendered directly in Markdown viewers that support Mermaid.

```mermaid
flowchart LR
    %% -------------------------
    %% Operations / Control
    %% -------------------------
    subgraph OPS[Operations & Scheduling]
        OP1[Operator / Scheduler]
        OP2[Pre-check scripts\nkeyvault_secret_test\nsql_connection_test\nsharepoint_auth_test\nspn_healthcheck_test]
        OP3[Python ingestion runner\npython -m sharepoint_ingest.main]
    end

    OP1 --> OP2
    OP1 --> OP3

    %% -------------------------
    %% DEV lane
    %% -------------------------
    subgraph DEV[DEV Environment]
        DEVKV[Azure Key Vault\n(dev secret set)]
        DEVSP[SharePoint Online\n/sites/data_ingest_dev]
        DEVSQL[(SQL Server\ningest_dev)]
        DEVCFG[(config.sharepoint_ingestion)]
        DEVLOG[(log.sharepoint_ingestion_audit)]
    end

    %% -------------------------
    %% PROD lane
    %% -------------------------
    subgraph PROD[PROD Environment]
        PRODKV[Azure Key Vault\n(prod secret set)]
        PRODSP[SharePoint Online\nprod site collection]
        PRODSQL[(SQL Server\ningest_prod)]
        PRODCFG[(config.sharepoint_ingestion)]
        PRODLOG[(log.sharepoint_ingestion_audit)]
    end

    %% DEV calls
    OP2 -->|--env dev| DEVKV
    OP2 -->|--env dev| DEVSP
    OP2 -->|--env dev| DEVSQL

    OP3 -->|--env dev| DEVKV
    OP3 -->|--env dev| DEVSP
    OP3 -->|--env dev| DEVSQL
    OP3 --> DEVCFG
    OP3 --> DEVLOG

    %% PROD calls
    OP2 -->|--env prod| PRODKV
    OP2 -->|--env prod| PRODSP
    OP2 -->|--env prod| PRODSQL

    OP3 -->|--env prod| PRODKV
    OP3 -->|--env prod| PRODSP
    OP3 -->|--env prod| PRODSQL
    OP3 --> PRODCFG
    OP3 --> PRODLOG
```

## Azure image artifact mapping (for Visio / draw.io)

When converting this into a polished Visio-style diagram, use Azure/Microsoft icons for:

- **Azure Key Vault** for `DEVKV` / `PRODKV`
- **Microsoft 365 / SharePoint** for `DEVSP` / `PRODSP`
- **SQL Server / Azure SQL-style DB artifact** for `DEVSQL` / `PRODSQL`
- **Microsoft Entra ID** (optional overlay) to depict app registration / SPN trust path

## Connectivity and port expectations

- Python -> Azure Key Vault: **HTTPS 443**
- Python -> SharePoint Online: **HTTPS 443**
- Python -> SQL Server: **TDS over TCP 1433**
