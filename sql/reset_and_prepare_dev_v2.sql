-- =============================================================================
-- reset_and_prepare_dev_v2.sql  —  SharePoint Ingestion Platform  —  DEV ONLY
-- =============================================================================
-- Targets the three-database DEV model:
--   ingest_audit_dev  — config.* + log.*
--   ingest_stg_dev    — sharepoint.dest_*  (daily truncate-and-load landing)
--   ingest_int_dev    — sharepoint.dest_*  (promoted / integrated data)
--
-- SAFETY: All sections contain DB_NAME() guard-rails.
-- Run as a DBA / db_owner login against the local SQL instance.
-- =============================================================================

-- ===========================================================================
-- SECTION 1  —  Reset ingest_audit_dev
--              Clear audit log + config, then re-seed TEST-scope config rows
-- ===========================================================================

USE ingest_audit_dev;
GO

IF DB_NAME() <> 'ingest_audit_dev'
BEGIN
    RAISERROR('Guard: this section must run against ingest_audit_dev only.', 16, 1);
    RETURN;
END
GO

DELETE FROM log.sharepoint_ingestion_audit;
GO

DELETE FROM config.sharepoint_ingestion;
GO

-- ── wf-valid-customers ───────────────────────────────────────────────────────
INSERT INTO config.sharepoint_ingestion (
    sharepoint_base_url,
    sharepoint_process_folder,
    excel_tab_name,
    sharepoint_process_archive_folder,
    sharepoint_process_failed_folder,
    process_frequency,
    header_skip_rows,
    check_source_dest_columns,
    multi_file_ingest,
    to_email_address,
    cc_email_address,
    process_id,
    workflow_id,
    staging_table_name,
    integrated_table_name,
    is_active,
    ingestion_scope,
    is_test_data,
    file_name_pattern,
    load_strategy,
    merge_key_columns,
    column_mapping_json
)
VALUES (
    'https://mycompany715.sharepoint.com/sites/data_ingest_dev',
    '/Documents/valid_customers',
    'ALL_SHEETS',
    '/Documents/valid_customers/Processed',
    '/Documents/valid_customers/Failed',
    'OnDemand',
    0,
    '1',
    '1',
    'NathanChapman@company715.onmicrosoft.com',
    NULL,
    NEWID(),
    'wf-valid-customers',
    'sharepoint.dest_customers',
    'sharepoint.dest_customers',
    '1',
    'TEST',
    1,
    'valid_customers_*.xlsx',
    'APPEND',
    'customer_id,excel_tab_name',
    '{"CustomerId":"customer_id","CustomerName":"customer_name","SignupDate":"signup_date","CreditLimit":"credit_limit","IsActive":"is_active","RegionCode":"region_code","SourceSystem":"source_system"}'
);
GO

-- ── wf-valid-transactions-standard ───────────────────────────────────────────
INSERT INTO config.sharepoint_ingestion (
    sharepoint_base_url,
    sharepoint_process_folder,
    excel_tab_name,
    sharepoint_process_archive_folder,
    sharepoint_process_failed_folder,
    process_frequency,
    header_skip_rows,
    check_source_dest_columns,
    multi_file_ingest,
    to_email_address,
    cc_email_address,
    process_id,
    workflow_id,
    staging_table_name,
    integrated_table_name,
    is_active,
    ingestion_scope,
    is_test_data,
    file_name_pattern,
    load_strategy,
    merge_key_columns,
    column_mapping_json
)
VALUES (
    'https://mycompany715.sharepoint.com/sites/data_ingest_dev',
    '/Documents/valid_transactions',
    '',
    '/Documents/valid_transactions/Processed',
    '/Documents/valid_transactions/Failed',
    'OnDemand',
    0,
    '1',
    '1',
    'NathanChapman@company715.onmicrosoft.com',
    NULL,
    NEWID(),
    'wf-valid-transactions-standard',
    'sharepoint.dest_transactions',
    'sharepoint.dest_transactions',
    '1',
    'TEST',
    1,
    'valid_transactions_00[12].csv',
    'APPEND',
    'transaction_id',
    '{"TransactionId":"transaction_id","CustomerId":"customer_id","TransactionDate":"transaction_date","Amount":"amount","Currency":"currency","Status":"status"}'
);
GO

-- ── wf-valid-transactions-parquet ─────────────────────────────────────────────
INSERT INTO config.sharepoint_ingestion (
    sharepoint_base_url,
    sharepoint_process_folder,
    excel_tab_name,
    sharepoint_process_archive_folder,
    sharepoint_process_failed_folder,
    process_frequency,
    header_skip_rows,
    check_source_dest_columns,
    multi_file_ingest,
    to_email_address,
    cc_email_address,
    process_id,
    workflow_id,
    staging_table_name,
    integrated_table_name,
    is_active,
    ingestion_scope,
    is_test_data,
    file_name_pattern,
    load_strategy,
    merge_key_columns,
    column_mapping_json
)
VALUES (
    'https://mycompany715.sharepoint.com/sites/data_ingest_dev',
    '/Documents/valid_parquet',
    '',
    '/Documents/valid_parquet/Processed',
    '/Documents/valid_parquet/Failed',
    'OnDemand',
    0,
    '0',    -- check_source_dest_columns=0: parquet files may have varying optional columns (e.g. notes)
    '1',
    'NathanChapman@company715.onmicrosoft.com',
    NULL,
    NEWID(),
    'wf-valid-transactions-parquet',
    'sharepoint.dest_transactions_parquet',
    'sharepoint.dest_transactions_parquet',
    '1',
    'TEST',
    1,
    'valid_transactions_parquet_*.parquet',
    'APPEND',
    'transaction_id',
    '{"TransactionId":"transaction_id","CustomerId":"customer_id","TransactionDate":"transaction_date","Amount":"amount","Currency":"currency","Status":"status","SourceSystem":"source_system"}'
);
GO

-- ── wf-valid-transactions-large ───────────────────────────────────────────────
INSERT INTO config.sharepoint_ingestion (
    sharepoint_base_url,
    sharepoint_process_folder,
    excel_tab_name,
    sharepoint_process_archive_folder,
    sharepoint_process_failed_folder,
    process_frequency,
    header_skip_rows,
    check_source_dest_columns,
    multi_file_ingest,
    to_email_address,
    cc_email_address,
    process_id,
    workflow_id,
    staging_table_name,
    integrated_table_name,
    is_active,
    ingestion_scope,
    is_test_data,
    file_name_pattern,
    load_strategy,
    merge_key_columns,
    column_mapping_json
)
VALUES (
    'https://mycompany715.sharepoint.com/sites/data_ingest_dev',
    '/Documents/valid_transactions_large',
    '',
    '/Documents/valid_transactions_large/Processed',
    '/Documents/valid_transactions_large/Failed',
    'OnDemand',
    2,
    '1',
    '0',
    'NathanChapman@company715.onmicrosoft.com',
    NULL,
    NEWID(),
    'wf-valid-transactions-large',
    'sharepoint.dest_transactions_large',
    'sharepoint.dest_transactions_large',
    '1',
    'TEST',
    1,
    'valid_transactions_large.csv',
    'TRUNCATE',
    'transaction_id',
    '{"TransactionId":"transaction_id","CustomerId":"customer_id","TransactionDate":"transaction_date","Amount":"amount","Currency":"currency","Status":"status","Quantity":"quantity","DiscountRate":"discount_rate","FeeAmount":"fee_amount","TaxAmount":"tax_amount","NetAmount":"net_amount","Channel":"channel","Region":"region","SourceSystem":"source_system","BatchId":"batch_id","EventTimestamp":"event_timestamp","IsPriority":"is_priority","ReferenceCode":"reference_code","LedgerCode":"ledger_code","CommentText":"comment_text"}'
);
GO

-- ── wf-invalid-csv-all ────────────────────────────────────────────────────────
INSERT INTO config.sharepoint_ingestion (
    sharepoint_base_url,
    sharepoint_process_folder,
    excel_tab_name,
    sharepoint_process_archive_folder,
    sharepoint_process_failed_folder,
    process_frequency,
    header_skip_rows,
    check_source_dest_columns,
    multi_file_ingest,
    to_email_address,
    cc_email_address,
    process_id,
    workflow_id,
    staging_table_name,
    integrated_table_name,
    is_active,
    ingestion_scope,
    is_test_data,
    file_name_pattern,
    load_strategy,
    merge_key_columns,
    column_mapping_json
)
VALUES (
    'https://mycompany715.sharepoint.com/sites/data_ingest_dev',
    '/Documents/invalid_csv',
    '',
    '/Documents/invalid_csv/Processed',
    '/Documents/invalid_csv/Failed',
    'OnDemand',
    0,
    '1',
    '1',
    'NathanChapman@company715.onmicrosoft.com',
    NULL,
    NEWID(),
    'wf-invalid-csv-all',
    'sharepoint.dest_invalid_csv',
    'sharepoint.dest_invalid_csv',
    '1',
    'TEST',
    1,
    'invalid_*.csv',
    'APPEND',
    'transaction_id',
    '{"TransactionId":"transaction_id","CustomerId":"customer_id","TransactionDate":"transaction_date","Amount":"amount","Currency":"currency","Status":"status"}'
);
GO

-- ── wf-invalid-excel-all ──────────────────────────────────────────────────────
INSERT INTO config.sharepoint_ingestion (
    sharepoint_base_url,
    sharepoint_process_folder,
    excel_tab_name,
    sharepoint_process_archive_folder,
    sharepoint_process_failed_folder,
    process_frequency,
    header_skip_rows,
    check_source_dest_columns,
    multi_file_ingest,
    to_email_address,
    cc_email_address,
    process_id,
    workflow_id,
    staging_table_name,
    integrated_table_name,
    is_active,
    ingestion_scope,
    is_test_data,
    file_name_pattern,
    load_strategy,
    merge_key_columns,
    column_mapping_json
)
VALUES (
    'https://mycompany715.sharepoint.com/sites/data_ingest_dev',
    '/Documents/invalid_excel',
    'ALL_SHEETS',
    '/Documents/invalid_excel/Processed',
    '/Documents/invalid_excel/Failed',
    'OnDemand',
    0,
    '1',
    '1',
    'NathanChapman@company715.onmicrosoft.com',
    NULL,
    NEWID(),
    'wf-invalid-excel-all',
    'sharepoint.dest_invalid_excel',
    'sharepoint.dest_invalid_excel',
    '1',
    'TEST',
    1,
    'invalid_[acdn]*.xlsx',
    'APPEND',
    'customer_id,excel_tab_name',
    '{"CustomerId":"customer_id","CustomerName":"customer_name","SignupDate":"signup_date","CreditLimit":"credit_limit","IsActive":"is_active","RegionCode":"region_code","SourceSystem":"source_system"}'
);
GO

-- ── wf-invalid-excel-missing-tabs ────────────────────────────────────────────
INSERT INTO config.sharepoint_ingestion (
    sharepoint_base_url,
    sharepoint_process_folder,
    excel_tab_name,
    sharepoint_process_archive_folder,
    sharepoint_process_failed_folder,
    process_frequency,
    header_skip_rows,
    check_source_dest_columns,
    multi_file_ingest,
    to_email_address,
    cc_email_address,
    process_id,
    workflow_id,
    staging_table_name,
    integrated_table_name,
    is_active,
    ingestion_scope,
    is_test_data,
    file_name_pattern,
    load_strategy,
    merge_key_columns,
    column_mapping_json
)
VALUES (
    'https://mycompany715.sharepoint.com/sites/data_ingest_dev',
    '/Documents/invalid_excel',
    'Customers_AU',
    '/Documents/invalid_excel/Processed',
    '/Documents/invalid_excel/Failed',
    'OnDemand',
    0,
    '1',
    '1',
    'NathanChapman@company715.onmicrosoft.com',
    NULL,
    NEWID(),
    'wf-invalid-excel-missing-tabs',
    'sharepoint.dest_invalid_excel',
    'sharepoint.dest_invalid_excel',
    '1',
    'TEST',
    1,
    'invalid_missing_tabs.xlsx',
    'APPEND',
    'customer_id,excel_tab_name',
    '{"CustomerId":"customer_id","CustomerName":"customer_name","SignupDate":"signup_date","CreditLimit":"credit_limit","IsActive":"is_active","RegionCode":"region_code","SourceSystem":"source_system"}'
);
GO

-- ── wf-invalid-parquet-all ────────────────────────────────────────────────────
INSERT INTO config.sharepoint_ingestion (
    sharepoint_base_url,
    sharepoint_process_folder,
    excel_tab_name,
    sharepoint_process_archive_folder,
    sharepoint_process_failed_folder,
    process_frequency,
    header_skip_rows,
    check_source_dest_columns,
    multi_file_ingest,
    to_email_address,
    cc_email_address,
    process_id,
    workflow_id,
    staging_table_name,
    integrated_table_name,
    is_active,
    ingestion_scope,
    is_test_data,
    file_name_pattern,
    load_strategy,
    merge_key_columns,
    column_mapping_json
)
VALUES (
    'https://mycompany715.sharepoint.com/sites/data_ingest_dev',
    '/Documents/invalid_parquet',
    '',
    '/Documents/invalid_parquet/Processed',
    '/Documents/invalid_parquet/Failed',
    'OnDemand',
    0,
    '1',
    '1',
    'NathanChapman@company715.onmicrosoft.com',
    NULL,
    NEWID(),
    'wf-invalid-parquet-all',
    'sharepoint.dest_invalid_parquet',
    'sharepoint.dest_invalid_parquet',
    '1',
    'TEST',
    1,
    'invalid_*parquet*.parquet',
    'APPEND',
    'transaction_id',
    '{"TransactionId":"transaction_id","CustomerId":"customer_id","TransactionDate":"transaction_date","Amount":"amount","Currency":"currency","Status":"status","SourceSystem":"source_system"}'
);
GO

-- Verify seeded config rows
SELECT id, workflow_id, staging_table_name, ingestion_scope, is_active, file_name_pattern
FROM config.sharepoint_ingestion
ORDER BY id;
GO

-- ===========================================================================
-- SECTION 2  —  Recreate destination tables in ingest_stg_dev
-- ===========================================================================

USE ingest_stg_dev;
GO

IF DB_NAME() <> 'ingest_stg_dev'
BEGIN
    RAISERROR('Guard: this section must run against ingest_stg_dev only.', 16, 1);
    RETURN;
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'sharepoint')
    EXEC('CREATE SCHEMA [sharepoint] AUTHORIZATION [dbo]');
GO

-- Drop + recreate all destination tables (clean slate)

IF OBJECT_ID('sharepoint.dest_customers', 'U') IS NOT NULL DROP TABLE sharepoint.dest_customers;
GO
IF OBJECT_ID('sharepoint.dest_transactions', 'U') IS NOT NULL DROP TABLE sharepoint.dest_transactions;
GO
IF OBJECT_ID('sharepoint.dest_transactions_parquet', 'U') IS NOT NULL DROP TABLE sharepoint.dest_transactions_parquet;
GO
IF OBJECT_ID('sharepoint.dest_transactions_large', 'U') IS NOT NULL DROP TABLE sharepoint.dest_transactions_large;
GO
IF OBJECT_ID('sharepoint.dest_invalid_csv', 'U') IS NOT NULL DROP TABLE sharepoint.dest_invalid_csv;
GO
IF OBJECT_ID('sharepoint.dest_invalid_excel', 'U') IS NOT NULL DROP TABLE sharepoint.dest_invalid_excel;
GO
IF OBJECT_ID('sharepoint.dest_invalid_parquet', 'U') IS NOT NULL DROP TABLE sharepoint.dest_invalid_parquet;
GO

-- ── sharepoint.dest_customers ─────────────────────────────────────────────────
CREATE TABLE sharepoint.dest_customers (
    customer_id          VARCHAR(20)    NOT NULL,
    customer_name        VARCHAR(200)   NULL,
    signup_date          DATE           NULL,
    credit_limit         DECIMAL(18,2)  NULL,
    is_active            VARCHAR(1)     NULL,
    region_code          VARCHAR(10)    NULL,
    source_system        VARCHAR(50)    NULL,
    source_file_name     VARCHAR(255)   NULL,
    excel_tab_name       VARCHAR(100)   NOT NULL DEFAULT '',
    sp_ingest_load_dt    DATETIME       NOT NULL DEFAULT GETUTCDATE(),
    audit_id             BIGINT         NULL,
    [__$batch_id]        INT            NULL,
    [__$job_instance_id] INT            NULL,
    CONSTRAINT PK_stg_dev_dest_customers PRIMARY KEY (customer_id, excel_tab_name)
);
GO

-- ── sharepoint.dest_transactions ───────────────────────────────────────────────
CREATE TABLE sharepoint.dest_transactions (
    transaction_id       VARCHAR(20)    NOT NULL,
    customer_id          VARCHAR(20)    NULL,
    transaction_date     DATE           NULL,
    amount               DECIMAL(18,2)  NULL,
    currency             VARCHAR(10)    NULL,
    status               VARCHAR(20)    NULL,
    source_file_name     VARCHAR(255)   NULL,
    sp_ingest_load_dt    DATETIME       NOT NULL DEFAULT GETUTCDATE(),
    audit_id             BIGINT         NULL,
    [__$batch_id]        INT            NULL,
    [__$job_instance_id] INT            NULL,
    CONSTRAINT PK_stg_dev_dest_transactions PRIMARY KEY (transaction_id)
);
GO

-- ── sharepoint.dest_transactions_parquet ──────────────────────────────────────
CREATE TABLE sharepoint.dest_transactions_parquet (
    transaction_id       VARCHAR(20)    NOT NULL,
    customer_id          VARCHAR(20)    NULL,
    transaction_date     DATE           NULL,
    amount               DECIMAL(18,2)  NULL,
    currency             VARCHAR(10)    NULL,
    status               VARCHAR(20)    NULL,
    source_system        VARCHAR(50)    NULL,
    notes                VARCHAR(MAX)   NULL,
    source_file_name     VARCHAR(255)   NULL,
    sp_ingest_load_dt    DATETIME       NOT NULL DEFAULT GETUTCDATE(),
    audit_id             BIGINT         NULL,
    [__$batch_id]        INT            NULL,
    [__$job_instance_id] INT            NULL,
    CONSTRAINT PK_stg_dev_dest_transactions_parquet PRIMARY KEY (transaction_id)
);
GO

-- ── sharepoint.dest_transactions_large ────────────────────────────────────────
CREATE TABLE sharepoint.dest_transactions_large (
    transaction_id       VARCHAR(20)    NOT NULL,
    customer_id          VARCHAR(20)    NULL,
    transaction_date     DATE           NULL,
    amount               DECIMAL(18,2)  NULL,
    currency             VARCHAR(10)    NULL,
    status               VARCHAR(20)    NULL,
    quantity             INT            NULL,
    discount_rate        DECIMAL(9,4)   NULL,
    fee_amount           DECIMAL(18,2)  NULL,
    tax_amount           DECIMAL(18,2)  NULL,
    net_amount           DECIMAL(18,2)  NULL,
    channel              VARCHAR(20)    NULL,
    region               VARCHAR(10)    NULL,
    source_system        VARCHAR(50)    NULL,
    batch_id             VARCHAR(50)    NULL,
    event_timestamp      DATETIME2      NULL,
    is_priority          VARCHAR(1)     NULL,
    reference_code       VARCHAR(50)    NULL,
    ledger_code          VARCHAR(20)    NULL,
    comment_text         VARCHAR(500)   NULL,
    source_file_name     VARCHAR(255)   NULL,
    sp_ingest_load_dt    DATETIME       NOT NULL DEFAULT GETUTCDATE(),
    audit_id             BIGINT         NULL,
    [__$batch_id]        INT            NULL,
    [__$job_instance_id] INT            NULL,
    CONSTRAINT PK_stg_dev_dest_transactions_large PRIMARY KEY (transaction_id)
);
GO

-- ── sharepoint.dest_invalid_csv ───────────────────────────────────────────────
CREATE TABLE sharepoint.dest_invalid_csv (
    transaction_id       VARCHAR(20)    NOT NULL,
    customer_id          VARCHAR(20)    NULL,
    transaction_date     DATE           NULL,
    amount               DECIMAL(18,2)  NULL,
    currency             VARCHAR(10)    NULL,
    status               VARCHAR(20)    NULL,
    source_file_name     VARCHAR(255)   NULL,
    sp_ingest_load_dt    DATETIME       NOT NULL DEFAULT GETUTCDATE(),
    audit_id             BIGINT         NULL,
    [__$batch_id]        INT            NULL,
    [__$job_instance_id] INT            NULL,
    CONSTRAINT PK_stg_dev_dest_invalid_csv PRIMARY KEY (transaction_id)
);
GO

-- ── sharepoint.dest_invalid_excel ─────────────────────────────────────────────
CREATE TABLE sharepoint.dest_invalid_excel (
    customer_id          VARCHAR(20)    NOT NULL,
    customer_name        VARCHAR(200)   NULL,
    signup_date          DATE           NULL,
    credit_limit         DECIMAL(18,2)  NULL,
    is_active            VARCHAR(1)     NULL,
    region_code          VARCHAR(10)    NULL,
    source_system        VARCHAR(50)    NULL,
    source_file_name     VARCHAR(255)   NULL,
    excel_tab_name       VARCHAR(100)   NOT NULL DEFAULT '',
    sp_ingest_load_dt    DATETIME       NOT NULL DEFAULT GETUTCDATE(),
    audit_id             BIGINT         NULL,
    [__$batch_id]        INT            NULL,
    [__$job_instance_id] INT            NULL,
    CONSTRAINT PK_stg_dev_dest_invalid_excel PRIMARY KEY (customer_id, excel_tab_name)
);
GO

-- ── sharepoint.dest_invalid_parquet ───────────────────────────────────────────
-- status is VARCHAR(10): test artifact deliberately generates an over-length value
-- ("STATUS_VALUE_EXCEEDS_LIMIT") to trigger a schema validation failure.
CREATE TABLE sharepoint.dest_invalid_parquet (
    transaction_id       VARCHAR(20)    NOT NULL,
    customer_id          VARCHAR(20)    NULL,
    transaction_date     DATE           NULL,
    amount               DECIMAL(18,2)  NULL,
    currency             VARCHAR(10)    NULL,
    status               VARCHAR(10)    NULL,
    source_system        VARCHAR(50)    NULL,
    source_file_name     VARCHAR(255)   NULL,
    sp_ingest_load_dt    DATETIME       NOT NULL DEFAULT GETUTCDATE(),
    audit_id             BIGINT         NULL,
    [__$batch_id]        INT            NULL,
    [__$job_instance_id] INT            NULL,
    CONSTRAINT PK_stg_dev_dest_invalid_parquet PRIMARY KEY (transaction_id)
);
GO

-- Verify staging tables
SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = 'sharepoint' ORDER BY TABLE_NAME;
GO

-- ===========================================================================
-- SECTION 3  —  Recreate destination tables in ingest_int_dev
--              (identical structure to stg, separate PK constraint names)
-- ===========================================================================

USE ingest_int_dev;
GO

IF DB_NAME() <> 'ingest_int_dev'
BEGIN
    RAISERROR('Guard: this section must run against ingest_int_dev only.', 16, 1);
    RETURN;
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'sharepoint')
    EXEC('CREATE SCHEMA [sharepoint] AUTHORIZATION [dbo]');
GO

IF OBJECT_ID('sharepoint.dest_customers', 'U') IS NOT NULL DROP TABLE sharepoint.dest_customers;
GO
IF OBJECT_ID('sharepoint.dest_transactions', 'U') IS NOT NULL DROP TABLE sharepoint.dest_transactions;
GO
IF OBJECT_ID('sharepoint.dest_transactions_parquet', 'U') IS NOT NULL DROP TABLE sharepoint.dest_transactions_parquet;
GO
IF OBJECT_ID('sharepoint.dest_transactions_large', 'U') IS NOT NULL DROP TABLE sharepoint.dest_transactions_large;
GO
IF OBJECT_ID('sharepoint.dest_invalid_csv', 'U') IS NOT NULL DROP TABLE sharepoint.dest_invalid_csv;
GO
IF OBJECT_ID('sharepoint.dest_invalid_excel', 'U') IS NOT NULL DROP TABLE sharepoint.dest_invalid_excel;
GO
IF OBJECT_ID('sharepoint.dest_invalid_parquet', 'U') IS NOT NULL DROP TABLE sharepoint.dest_invalid_parquet;
GO

CREATE TABLE sharepoint.dest_customers (
    customer_id          VARCHAR(20)    NOT NULL,
    customer_name        VARCHAR(200)   NULL,
    signup_date          DATE           NULL,
    credit_limit         DECIMAL(18,2)  NULL,
    is_active            VARCHAR(1)     NULL,
    region_code          VARCHAR(10)    NULL,
    source_system        VARCHAR(50)    NULL,
    excel_tab_name       VARCHAR(100)   NOT NULL DEFAULT '',
    source_file_name     VARCHAR(255)   NULL,
    sp_ingest_load_dt    DATETIME       NOT NULL DEFAULT GETUTCDATE(),
    audit_id             BIGINT         NULL,
    [__$batch_id]        INT            NULL,
    [__$job_instance_id] INT            NULL,
    CONSTRAINT PK_int_dev_dest_customers PRIMARY KEY (customer_id, excel_tab_name)
);
GO

CREATE TABLE sharepoint.dest_transactions (
    transaction_id       VARCHAR(20)    NOT NULL,
    customer_id          VARCHAR(20)    NULL,
    transaction_date     DATE           NULL,
    amount               DECIMAL(18,2)  NULL,
    currency             VARCHAR(10)    NULL,
    status               VARCHAR(20)    NULL,
    source_file_name     VARCHAR(255)   NULL,
    sp_ingest_load_dt    DATETIME       NOT NULL DEFAULT GETUTCDATE(),
    audit_id             BIGINT         NULL,
    [__$batch_id]        INT            NULL,
    [__$job_instance_id] INT            NULL,
    CONSTRAINT PK_int_dev_dest_transactions PRIMARY KEY (transaction_id)
);
GO

CREATE TABLE sharepoint.dest_transactions_parquet (
    transaction_id       VARCHAR(20)    NOT NULL,
    customer_id          VARCHAR(20)    NULL,
    transaction_date     DATE           NULL,
    amount               DECIMAL(18,2)  NULL,
    currency             VARCHAR(10)    NULL,
    status               VARCHAR(20)    NULL,
    source_system        VARCHAR(50)    NULL,
    notes                VARCHAR(MAX)   NULL,
    source_file_name     VARCHAR(255)   NULL,
    sp_ingest_load_dt    DATETIME       NOT NULL DEFAULT GETUTCDATE(),
    audit_id             BIGINT         NULL,
    [__$batch_id]        INT            NULL,
    [__$job_instance_id] INT            NULL,
    CONSTRAINT PK_int_dev_dest_transactions_parquet PRIMARY KEY (transaction_id)
);
GO

CREATE TABLE sharepoint.dest_transactions_large (
    transaction_id       VARCHAR(20)    NOT NULL,
    customer_id          VARCHAR(20)    NULL,
    transaction_date     DATE           NULL,
    amount               DECIMAL(18,2)  NULL,
    currency             VARCHAR(10)    NULL,
    status               VARCHAR(20)    NULL,
    quantity             INT            NULL,
    discount_rate        DECIMAL(9,4)   NULL,
    fee_amount           DECIMAL(18,2)  NULL,
    tax_amount           DECIMAL(18,2)  NULL,
    net_amount           DECIMAL(18,2)  NULL,
    channel              VARCHAR(20)    NULL,
    region               VARCHAR(10)    NULL,
    source_system        VARCHAR(50)    NULL,
    batch_id             VARCHAR(50)    NULL,
    event_timestamp      DATETIME2      NULL,
    is_priority          VARCHAR(1)     NULL,
    reference_code       VARCHAR(50)    NULL,
    ledger_code          VARCHAR(20)    NULL,
    comment_text         VARCHAR(500)   NULL,
    source_file_name     VARCHAR(255)   NULL,
    sp_ingest_load_dt    DATETIME       NOT NULL DEFAULT GETUTCDATE(),
    audit_id             BIGINT         NULL,
    [__$batch_id]        INT            NULL,
    [__$job_instance_id] INT            NULL,
    CONSTRAINT PK_int_dev_dest_transactions_large PRIMARY KEY (transaction_id)
);
GO

CREATE TABLE sharepoint.dest_invalid_csv (
    transaction_id       VARCHAR(20)    NOT NULL,
    customer_id          VARCHAR(20)    NULL,
    transaction_date     DATE           NULL,
    amount               DECIMAL(18,2)  NULL,
    currency             VARCHAR(10)    NULL,
    status               VARCHAR(20)    NULL,
    source_file_name     VARCHAR(255)   NULL,
    sp_ingest_load_dt    DATETIME       NOT NULL DEFAULT GETUTCDATE(),
    audit_id             BIGINT         NULL,
    [__$batch_id]        INT            NULL,
    [__$job_instance_id] INT            NULL,
    CONSTRAINT PK_int_dev_dest_invalid_csv PRIMARY KEY (transaction_id)
);
GO

CREATE TABLE sharepoint.dest_invalid_excel (
    customer_id          VARCHAR(20)    NOT NULL,
    customer_name        VARCHAR(200)   NULL,
    signup_date          DATE           NULL,
    credit_limit         DECIMAL(18,2)  NULL,
    is_active            VARCHAR(1)     NULL,
    region_code          VARCHAR(10)    NULL,
    source_system        VARCHAR(50)    NULL,
    excel_tab_name       VARCHAR(100)   NOT NULL DEFAULT '',
    source_file_name     VARCHAR(255)   NULL,
    sp_ingest_load_dt    DATETIME       NOT NULL DEFAULT GETUTCDATE(),
    audit_id             BIGINT         NULL,
    [__$batch_id]        INT            NULL,
    [__$job_instance_id] INT            NULL,
    CONSTRAINT PK_int_dev_dest_invalid_excel PRIMARY KEY (customer_id, excel_tab_name)
);
GO

CREATE TABLE sharepoint.dest_invalid_parquet (
    transaction_id       VARCHAR(20)    NOT NULL,
    customer_id          VARCHAR(20)    NULL,
    transaction_date     DATE           NULL,
    amount               DECIMAL(18,2)  NULL,
    currency             VARCHAR(10)    NULL,
    status               VARCHAR(10)    NULL,
    source_system        VARCHAR(50)    NULL,
    source_file_name     VARCHAR(255)   NULL,
    sp_ingest_load_dt    DATETIME       NOT NULL DEFAULT GETUTCDATE(),
    audit_id             BIGINT         NULL,
    [__$batch_id]        INT            NULL,
    [__$job_instance_id] INT            NULL,
    CONSTRAINT PK_int_dev_dest_invalid_parquet PRIMARY KEY (transaction_id)
);
GO

-- Verify integrated tables
SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = 'sharepoint' ORDER BY TABLE_NAME;
GO
