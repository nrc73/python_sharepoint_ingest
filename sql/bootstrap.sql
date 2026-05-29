-- =============================================================================
-- bootstrap.sql  —  SharePoint Ingestion Platform  —  ALL environments
-- =============================================================================
-- Applies to SQL Server (local/Azure SQL).
-- Run as a DBA / db_owner login.
--
-- DATABASE LAYOUT
-- ───────────────────────────────────────────────────────────────────────────────
-- ingest_audit_prod / ingest_audit_dev
--     config.sharepoint_ingestion   — ingestion configuration
--     log.sharepoint_ingestion_audit — per-file audit rows
--
-- ingest_stg_prod  / ingest_stg_dev
--     sharepoint.*   — daily truncate-and-load landing tables
--                      (identical structure to int tables)
--
-- ingest_int_prod  / ingest_int_dev
--     sharepoint.*   — integrated (promoted) data tables
--
-- MIGRATION NOTES (existing ingest_prod / ingest_dev deployments)
-- ───────────────────────────────────────────────────────────────────────────────
-- The legacy databases ingest_prod and ingest_dev are superseded.
-- Rename / migrate via the steps at the end of this file.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1.  AUDIT DATABASES  (config + log)
-- ---------------------------------------------------------------------------

IF DB_ID('ingest_audit_prod') IS NULL
    CREATE DATABASE ingest_audit_prod;
GO
IF DB_ID('ingest_audit_dev') IS NULL
    CREATE DATABASE ingest_audit_dev;
GO

-- ---------------------------------------------------------------------------
-- 2.  STAGING DATABASES  (daily truncate-and-load landing)
-- ---------------------------------------------------------------------------

IF DB_ID('ingest_stg_prod') IS NULL
    CREATE DATABASE ingest_stg_prod;
GO
IF DB_ID('ingest_stg_dev') IS NULL
    CREATE DATABASE ingest_stg_dev;
GO

-- ---------------------------------------------------------------------------
-- 3.  INTEGRATED DATABASES  (promoted / stable data)
-- ---------------------------------------------------------------------------

IF DB_ID('ingest_int_prod') IS NULL
    CREATE DATABASE ingest_int_prod;
GO
IF DB_ID('ingest_int_dev') IS NULL
    CREATE DATABASE ingest_int_dev;
GO

-- ===========================================================================
--  Section A  —  AUDIT DB OBJECTS  (run against ingest_audit_prod)
-- ===========================================================================

USE ingest_audit_prod;
GO

IF DB_NAME() <> 'ingest_audit_prod'
BEGIN
    RAISERROR('Guard: this block must run against ingest_audit_prod.', 16, 1);
    RETURN;
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'config')
    EXEC('CREATE SCHEMA [config] AUTHORIZATION [dbo]');
GO

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'log')
    EXEC('CREATE SCHEMA [log] AUTHORIZATION [dbo]');
GO

-- ---------------------------------------------------------------------------
-- A1.  config.sharepoint_ingestion  (canonical column set)
-- ---------------------------------------------------------------------------

IF OBJECT_ID('config.sharepoint_ingestion', 'U') IS NULL
BEGIN
    CREATE TABLE config.sharepoint_ingestion (
        id                               INT IDENTITY(1,1) PRIMARY KEY,
        sharepoint_base_url              VARCHAR(500)   NOT NULL,
        sharepoint_process_folder        VARCHAR(200)   NOT NULL,
        excel_tab_name                   VARCHAR(100)   NOT NULL,
        sharepoint_process_archive_folder VARCHAR(200)  NULL,
        sharepoint_process_failed_folder VARCHAR(200)   NULL,
        process_frequency                VARCHAR(50)    NULL,
        header_skip_rows                 INT            NOT NULL DEFAULT 0,
        check_source_dest_columns        VARCHAR(1)     NULL,
        multi_file_ingest                VARCHAR(1)     NULL,
        -- email — new canonical names
        to_email_address                 VARCHAR(400)   NULL,
        cc_email_address                 VARCHAR(400)   NULL,
        process_id                       UNIQUEIDENTIFIER NULL,
        workflow_id                      VARCHAR(100)   NULL,
        -- destination tables
        staging_table_name               VARCHAR(200)   NOT NULL,    -- e.g. sharepoint.dest_customers
        integrated_table_name            VARCHAR(200)   NULL,        -- e.g. sharepoint.dest_customers (in int DB)
        is_active                        VARCHAR(1)     NOT NULL DEFAULT '1',
        file_name_pattern                VARCHAR(255)   NULL,
        load_strategy                    VARCHAR(30)    NULL,        -- TRUNCATE | APPEND
        merge_key_columns                VARCHAR(400)   NULL,
        column_mapping_json              VARCHAR(MAX)   NOT NULL CONSTRAINT DF_spi_column_mapping_json DEFAULT '{}',
        ingestion_scope                  VARCHAR(20)    NOT NULL CONSTRAINT DF_spi_scope DEFAULT 'REAL',
        is_test_data                     BIT            NOT NULL CONSTRAINT DF_spi_test_data DEFAULT 0,
        sp_ingest_created_utc            DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME(),
        sp_ingest_modified_utc           DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME()
    );
END
GO

-- ── Idempotent column additions (for in-place migration from old schema) ───

IF COL_LENGTH('config.sharepoint_ingestion', 'to_email_address') IS NULL
    ALTER TABLE config.sharepoint_ingestion ADD to_email_address VARCHAR(400) NULL;
GO

IF COL_LENGTH('config.sharepoint_ingestion', 'cc_email_address') IS NULL
    ALTER TABLE config.sharepoint_ingestion ADD cc_email_address VARCHAR(400) NULL;
GO

IF COL_LENGTH('config.sharepoint_ingestion', 'integrated_table_name') IS NULL
    ALTER TABLE config.sharepoint_ingestion ADD integrated_table_name VARCHAR(200) NULL;
GO

IF COL_LENGTH('config.sharepoint_ingestion', 'ingestion_scope') IS NULL
    ALTER TABLE config.sharepoint_ingestion
        ADD ingestion_scope VARCHAR(20) NOT NULL
            CONSTRAINT DF_spi_scope_mig DEFAULT 'REAL';
GO

IF COL_LENGTH('config.sharepoint_ingestion', 'is_test_data') IS NULL
    ALTER TABLE config.sharepoint_ingestion
        ADD is_test_data BIT NOT NULL
            CONSTRAINT DF_spi_test_data_mig DEFAULT 0;
GO

-- Ensure column_mapping_json is always populated with a JSON object
IF COL_LENGTH('config.sharepoint_ingestion', 'column_mapping_json') IS NULL
    ALTER TABLE config.sharepoint_ingestion
        ADD column_mapping_json VARCHAR(MAX) NULL;
GO

UPDATE config.sharepoint_ingestion
SET column_mapping_json = '{}'
WHERE column_mapping_json IS NULL
   OR LTRIM(RTRIM(column_mapping_json)) = '';
GO

IF OBJECT_ID('config.DF_spi_column_mapping_json', 'D') IS NULL
    ALTER TABLE config.sharepoint_ingestion
        ADD CONSTRAINT DF_spi_column_mapping_json DEFAULT '{}' FOR column_mapping_json;
GO

IF EXISTS (
    SELECT 1
    FROM sys.columns
    WHERE object_id = OBJECT_ID('config.sharepoint_ingestion')
      AND name = 'column_mapping_json'
      AND is_nullable = 1
)
    ALTER TABLE config.sharepoint_ingestion
        ALTER COLUMN column_mapping_json VARCHAR(MAX) NOT NULL;
GO

-- ── Migrate old email column values to new columns ─────────────────────────

UPDATE config.sharepoint_ingestion
SET
    to_email_address = COALESCE(
        NULLIF(LTRIM(RTRIM(to_email_address)), ''),
        NULLIF(LTRIM(RTRIM(error_notification_email_address)), '')
    ),
    cc_email_address = COALESCE(
        NULLIF(LTRIM(RTRIM(cc_email_address)), ''),
        NULLIF(LTRIM(RTRIM(error_notification_cc_email_address)), '')
    )
WHERE
    COL_LENGTH('config.sharepoint_ingestion', 'error_notification_email_address') IS NOT NULL;
GO

-- ── Drop legacy columns if they still exist ────────────────────────────────

IF COL_LENGTH('config.sharepoint_ingestion', 'error_notification_email_address') IS NOT NULL
    ALTER TABLE config.sharepoint_ingestion DROP COLUMN error_notification_email_address;
GO

IF COL_LENGTH('config.sharepoint_ingestion', 'error_notification_cc_email_address') IS NOT NULL
    ALTER TABLE config.sharepoint_ingestion DROP COLUMN error_notification_cc_email_address;
GO

IF COL_LENGTH('config.sharepoint_ingestion', 'ingestion_domain') IS NOT NULL
    ALTER TABLE config.sharepoint_ingestion DROP COLUMN ingestion_domain;
GO

-- ── Migrate staging_table_name schema prefix staging → sharepoint ──────────
-- (Reverses an earlier incorrect migration that renamed sharepoint.* → staging.*)

UPDATE config.sharepoint_ingestion
SET staging_table_name = 'sharepoint.' + SUBSTRING(staging_table_name, CHARINDEX('.', staging_table_name) + 1, 200)
WHERE staging_table_name LIKE 'staging.%';
GO

UPDATE config.sharepoint_ingestion
SET integrated_table_name = staging_table_name
WHERE integrated_table_name IS NULL OR LTRIM(RTRIM(integrated_table_name)) = '';
GO

-- ── Scope backfill ─────────────────────────────────────────────────────────

UPDATE config.sharepoint_ingestion
SET ingestion_scope = CASE
        WHEN is_test_data = 1 THEN 'TEST'
        ELSE 'REAL'
    END
WHERE ingestion_scope IS NULL OR LTRIM(RTRIM(ingestion_scope)) = '';
GO

-- ---------------------------------------------------------------------------
-- A2.  Seed row for ingest_audit_prod (skip if already seeded)
-- ---------------------------------------------------------------------------

IF NOT EXISTS (SELECT 1 FROM config.sharepoint_ingestion)
BEGIN
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
        'https://mycompany715.sharepoint.com/sites/data_ingestion_prod',
        '/sites/data_ingestion_prod/General/Input for ETL',
        'DATA',
        '/sites/data_ingestion_prod/General/Processed',
        '/sites/data_ingestion_prod/General/Failed',
        'Daily',
        0,
        '1',
        '1',
        'NathanChapman@company715.onmicrosoft.com',
        NULL,
        NEWID(),
        'workflow-sample-001',
        'sharepoint.sample_ingestion_target',
        'sharepoint.sample_ingestion_target',
        '1',
        'REAL',
        0,
        '*.csv',
        'TRUNCATE',
        'business_key',
        '{"BusinessKey":"business_key","Name":"name","Amount":"amount","EffectiveDate":"effective_date"}'
    );
END
GO

-- ---------------------------------------------------------------------------
-- A3.  log.sharepoint_ingestion_audit
-- ---------------------------------------------------------------------------

IF OBJECT_ID('log.sharepoint_ingestion_audit', 'U') IS NULL
BEGIN
    CREATE TABLE log.sharepoint_ingestion_audit (
        audit_id               BIGINT IDENTITY(1,1) PRIMARY KEY,
        config_id              INT              NOT NULL,
        workflow_id            VARCHAR(100)     NULL,
        process_id             UNIQUEIDENTIFIER NULL,
        file_name              VARCHAR(255)     NULL,
        destination_database   VARCHAR(128)     NULL,
        destination_table      VARCHAR(300)     NULL,
        status                 VARCHAR(20)      NOT NULL,
        records_loaded         INT              NULL,
        rows_scanned           INT              NULL,
        validation_error_count INT              NULL,
        memory_peak_mb         DECIMAL(18,2)    NULL,
        duration_seconds       DECIMAL(18,2)    NULL,
        ingestion_scope        VARCHAR(20)      NULL,
        is_test_data           BIT              NULL,
        message                VARCHAR(MAX)     NULL,
        sp_ingest_created_utc  DATETIME2        NOT NULL DEFAULT SYSUTCDATETIME()
    );
END
GO

-- Idempotent additions for in-place migration

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'rows_scanned') IS NULL
    ALTER TABLE log.sharepoint_ingestion_audit ADD rows_scanned INT NULL;
GO

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'validation_error_count') IS NULL
    ALTER TABLE log.sharepoint_ingestion_audit ADD validation_error_count INT NULL;
GO

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'memory_peak_mb') IS NULL
    ALTER TABLE log.sharepoint_ingestion_audit ADD memory_peak_mb DECIMAL(18,2) NULL;
GO

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'duration_seconds') IS NULL
    ALTER TABLE log.sharepoint_ingestion_audit ADD duration_seconds DECIMAL(18,2) NULL;
GO

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'ingestion_scope') IS NULL
    ALTER TABLE log.sharepoint_ingestion_audit ADD ingestion_scope VARCHAR(20) NULL;
GO

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'is_test_data') IS NULL
    ALTER TABLE log.sharepoint_ingestion_audit ADD is_test_data BIT NULL;
GO

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'destination_database') IS NULL
    ALTER TABLE log.sharepoint_ingestion_audit ADD destination_database VARCHAR(128) NULL;
GO

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'destination_table') IS NULL
    ALTER TABLE log.sharepoint_ingestion_audit ADD destination_table VARCHAR(300) NULL;
GO

-- Drop retired columns
IF COL_LENGTH('log.sharepoint_ingestion_audit', 'ingestion_domain') IS NOT NULL
    ALTER TABLE log.sharepoint_ingestion_audit DROP COLUMN ingestion_domain;
GO

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'batch_id') IS NOT NULL
    ALTER TABLE log.sharepoint_ingestion_audit DROP COLUMN batch_id;
GO

IF COLUMNPROPERTY(OBJECT_ID('log.sharepoint_ingestion_audit'), 'is_validated', 'ColumnId') IS NOT NULL
    ALTER TABLE log.sharepoint_ingestion_audit DROP COLUMN is_validated;
GO

-- ---------------------------------------------------------------------------
-- A4.  Guard-rail trigger on config.sharepoint_ingestion
-- ---------------------------------------------------------------------------

CREATE OR ALTER TRIGGER config.trg_guard_prod_sharepoint_ingestion
ON config.sharepoint_ingestion
AFTER INSERT, UPDATE
AS
BEGIN
    SET NOCOUNT ON;

    IF EXISTS (
        SELECT 1
        FROM inserted i
        WHERE
            ISNULL(i.is_test_data, 0) = 1
            OR UPPER(LTRIM(RTRIM(ISNULL(i.ingestion_scope, 'REAL')))) IN ('TEST', 'VALIDATION', 'PERF_TEST')
    )
    BEGIN
        RAISERROR(
            'Guard rail: TEST/VALIDATION/PERF_TEST config rows are blocked in PROD (config.sharepoint_ingestion).',
            16, 1
        );
        ROLLBACK TRANSACTION;
        RETURN;
    END
END
GO

-- ===========================================================================
--  Section B  —  STG + INT DB OBJECTS  (sharepoint schema)
-- ===========================================================================

-- Run the block below against BOTH ingest_stg_prod and ingest_int_prod.
-- (Repeat for dev equivalents: ingest_stg_dev / ingest_int_dev)

USE ingest_stg_prod;
GO

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'sharepoint')
    EXEC('CREATE SCHEMA [sharepoint] AUTHORIZATION [dbo]');
GO

-- Drop legacy staging schema objects if they still exist (one-time cleanup)
IF OBJECT_ID('staging.sample_ingestion_target', 'U') IS NOT NULL DROP TABLE staging.sample_ingestion_target;
GO

-- Sample destination table (staging DB copy)
IF OBJECT_ID('sharepoint.sample_ingestion_target', 'U') IS NULL
BEGIN
    CREATE TABLE sharepoint.sample_ingestion_target (
        business_key        VARCHAR(50)    NOT NULL,
        name                VARCHAR(200)   NULL,
        amount              DECIMAL(18,2)  NULL,
        effective_date      DATE           NULL,
        source_file_name    VARCHAR(255)   NULL,
        sp_ingest_created_utc DATETIME2    NOT NULL DEFAULT SYSUTCDATETIME(),
        sp_ingest_modified_utc DATETIME2   NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_stg_sample_ingestion_target PRIMARY KEY (business_key)
    );
END
GO

-- ── Integrated DB ─────────────────────────────────────────────────────────

USE ingest_int_prod;
GO

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'sharepoint')
    EXEC('CREATE SCHEMA [sharepoint] AUTHORIZATION [dbo]');
GO

-- Drop legacy staging schema objects if they still exist (one-time cleanup)
IF OBJECT_ID('staging.sample_ingestion_target', 'U') IS NOT NULL DROP TABLE staging.sample_ingestion_target;
GO

-- Sample destination table (integrated DB copy — identical structure to stg)
IF OBJECT_ID('sharepoint.sample_ingestion_target', 'U') IS NULL
BEGIN
    CREATE TABLE sharepoint.sample_ingestion_target (
        business_key        VARCHAR(50)    NOT NULL,
        name                VARCHAR(200)   NULL,
        amount              DECIMAL(18,2)  NULL,
        effective_date      DATE           NULL,
        source_file_name    VARCHAR(255)   NULL,
        sp_ingest_created_utc DATETIME2    NOT NULL DEFAULT SYSUTCDATETIME(),
        sp_ingest_modified_utc DATETIME2   NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_int_sample_ingestion_target PRIMARY KEY (business_key)
    );
END
GO

-- ===========================================================================
--  Section C  —  DEV equivalents  (mirror of A + B, against *_dev databases)
-- ===========================================================================

USE ingest_audit_dev;
GO

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'config')
    EXEC('CREATE SCHEMA [config] AUTHORIZATION [dbo]');
GO
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'log')
    EXEC('CREATE SCHEMA [log] AUTHORIZATION [dbo]');
GO

-- config.sharepoint_ingestion (dev)
IF OBJECT_ID('config.sharepoint_ingestion', 'U') IS NULL
BEGIN
    CREATE TABLE config.sharepoint_ingestion (
        id                               INT IDENTITY(1,1) PRIMARY KEY,
        sharepoint_base_url              VARCHAR(500)   NOT NULL,
        sharepoint_process_folder        VARCHAR(200)   NOT NULL,
        excel_tab_name                   VARCHAR(100)   NOT NULL,
        sharepoint_process_archive_folder VARCHAR(200)  NULL,
        sharepoint_process_failed_folder VARCHAR(200)   NULL,
        process_frequency                VARCHAR(50)    NULL,
        header_skip_rows                 INT            NOT NULL DEFAULT 0,
        check_source_dest_columns        VARCHAR(1)     NULL,
        multi_file_ingest                VARCHAR(1)     NULL,
        to_email_address                 VARCHAR(400)   NULL,
        cc_email_address                 VARCHAR(400)   NULL,
        process_id                       UNIQUEIDENTIFIER NULL,
        workflow_id                      VARCHAR(100)   NULL,
        staging_table_name               VARCHAR(200)   NOT NULL,
        integrated_table_name            VARCHAR(200)   NULL,
        is_active                        VARCHAR(1)     NOT NULL DEFAULT '1',
        file_name_pattern                VARCHAR(255)   NULL,
        load_strategy                    VARCHAR(30)    NULL,
        merge_key_columns                VARCHAR(400)   NULL,
        column_mapping_json              VARCHAR(MAX)   NOT NULL CONSTRAINT DF_spi_dev_column_mapping_json DEFAULT '{}',
        ingestion_scope                  VARCHAR(20)    NOT NULL CONSTRAINT DF_spi_dev_scope DEFAULT 'REAL',
        is_test_data                     BIT            NOT NULL CONSTRAINT DF_spi_dev_test_data DEFAULT 0,
        sp_ingest_created_utc            DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME(),
        sp_ingest_modified_utc           DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME()
    );
END
GO

-- Idempotent migration columns (dev)
IF COL_LENGTH('config.sharepoint_ingestion', 'to_email_address') IS NULL
    ALTER TABLE config.sharepoint_ingestion ADD to_email_address VARCHAR(400) NULL;
GO
IF COL_LENGTH('config.sharepoint_ingestion', 'cc_email_address') IS NULL
    ALTER TABLE config.sharepoint_ingestion ADD cc_email_address VARCHAR(400) NULL;
GO
IF COL_LENGTH('config.sharepoint_ingestion', 'integrated_table_name') IS NULL
    ALTER TABLE config.sharepoint_ingestion ADD integrated_table_name VARCHAR(200) NULL;
GO
IF COL_LENGTH('config.sharepoint_ingestion', 'ingestion_scope') IS NULL
    ALTER TABLE config.sharepoint_ingestion
        ADD ingestion_scope VARCHAR(20) NOT NULL CONSTRAINT DF_spi_dev_scope_mig DEFAULT 'REAL';
GO
IF COL_LENGTH('config.sharepoint_ingestion', 'is_test_data') IS NULL
    ALTER TABLE config.sharepoint_ingestion
        ADD is_test_data BIT NOT NULL CONSTRAINT DF_spi_dev_test_data_mig DEFAULT 0;
GO

-- Ensure column_mapping_json is always populated with a JSON object (dev)
IF COL_LENGTH('config.sharepoint_ingestion', 'column_mapping_json') IS NULL
    ALTER TABLE config.sharepoint_ingestion
        ADD column_mapping_json VARCHAR(MAX) NULL;
GO

UPDATE config.sharepoint_ingestion
SET column_mapping_json = '{}'
WHERE column_mapping_json IS NULL
   OR LTRIM(RTRIM(column_mapping_json)) = '';
GO

IF OBJECT_ID('config.DF_spi_dev_column_mapping_json', 'D') IS NULL
    ALTER TABLE config.sharepoint_ingestion
        ADD CONSTRAINT DF_spi_dev_column_mapping_json DEFAULT '{}' FOR column_mapping_json;
GO

IF EXISTS (
    SELECT 1
    FROM sys.columns
    WHERE object_id = OBJECT_ID('config.sharepoint_ingestion')
      AND name = 'column_mapping_json'
      AND is_nullable = 1
)
    ALTER TABLE config.sharepoint_ingestion
        ALTER COLUMN column_mapping_json VARCHAR(MAX) NOT NULL;
GO

-- Migrate old email values (dev)
IF COL_LENGTH('config.sharepoint_ingestion', 'error_notification_email_address') IS NOT NULL
BEGIN
    UPDATE config.sharepoint_ingestion
    SET to_email_address = COALESCE(NULLIF(LTRIM(RTRIM(to_email_address)), ''),
                                    NULLIF(LTRIM(RTRIM(error_notification_email_address)), '')),
        cc_email_address = COALESCE(NULLIF(LTRIM(RTRIM(cc_email_address)), ''),
                                    NULLIF(LTRIM(RTRIM(error_notification_cc_email_address)), ''));
END
GO
IF COL_LENGTH('config.sharepoint_ingestion', 'error_notification_email_address') IS NOT NULL
    ALTER TABLE config.sharepoint_ingestion DROP COLUMN error_notification_email_address;
GO
IF COL_LENGTH('config.sharepoint_ingestion', 'error_notification_cc_email_address') IS NOT NULL
    ALTER TABLE config.sharepoint_ingestion DROP COLUMN error_notification_cc_email_address;
GO
IF COL_LENGTH('config.sharepoint_ingestion', 'ingestion_domain') IS NOT NULL
    ALTER TABLE config.sharepoint_ingestion DROP COLUMN ingestion_domain;
GO

-- Migrate staging schema prefix staging → sharepoint (dev)
-- (Reverses an earlier incorrect migration that renamed sharepoint.* → staging.*)
UPDATE config.sharepoint_ingestion
SET staging_table_name = 'sharepoint.' + SUBSTRING(staging_table_name, CHARINDEX('.', staging_table_name) + 1, 200)
WHERE staging_table_name LIKE 'staging.%';
GO
UPDATE config.sharepoint_ingestion
SET integrated_table_name = staging_table_name
WHERE integrated_table_name IS NULL OR LTRIM(RTRIM(integrated_table_name)) = '';
GO

-- log.sharepoint_ingestion_audit (dev)
IF OBJECT_ID('log.sharepoint_ingestion_audit', 'U') IS NULL
BEGIN
    CREATE TABLE log.sharepoint_ingestion_audit (
        audit_id               BIGINT IDENTITY(1,1) PRIMARY KEY,
        config_id              INT              NOT NULL,
        workflow_id            VARCHAR(100)     NULL,
        process_id             UNIQUEIDENTIFIER NULL,
        file_name              VARCHAR(255)     NULL,
        destination_database   VARCHAR(128)     NULL,
        destination_table      VARCHAR(300)     NULL,
        status                 VARCHAR(20)      NOT NULL,
        records_loaded         INT              NULL,
        rows_scanned           INT              NULL,
        validation_error_count INT              NULL,
        memory_peak_mb         DECIMAL(18,2)    NULL,
        duration_seconds       DECIMAL(18,2)    NULL,
        ingestion_scope        VARCHAR(20)      NULL,
        is_test_data           BIT              NULL,
        message                VARCHAR(MAX)     NULL,
        sp_ingest_created_utc  DATETIME2        NOT NULL DEFAULT SYSUTCDATETIME()
    );
END
GO

-- Idempotent additions for in-place migration (dev audit DB)
IF COL_LENGTH('log.sharepoint_ingestion_audit', 'rows_scanned') IS NULL
    ALTER TABLE log.sharepoint_ingestion_audit ADD rows_scanned INT NULL;
GO
IF COL_LENGTH('log.sharepoint_ingestion_audit', 'validation_error_count') IS NULL
    ALTER TABLE log.sharepoint_ingestion_audit ADD validation_error_count INT NULL;
GO
IF COL_LENGTH('log.sharepoint_ingestion_audit', 'memory_peak_mb') IS NULL
    ALTER TABLE log.sharepoint_ingestion_audit ADD memory_peak_mb DECIMAL(18,2) NULL;
GO
IF COL_LENGTH('log.sharepoint_ingestion_audit', 'duration_seconds') IS NULL
    ALTER TABLE log.sharepoint_ingestion_audit ADD duration_seconds DECIMAL(18,2) NULL;
GO
IF COL_LENGTH('log.sharepoint_ingestion_audit', 'ingestion_scope') IS NULL
    ALTER TABLE log.sharepoint_ingestion_audit ADD ingestion_scope VARCHAR(20) NULL;
GO
IF COL_LENGTH('log.sharepoint_ingestion_audit', 'is_test_data') IS NULL
    ALTER TABLE log.sharepoint_ingestion_audit ADD is_test_data BIT NULL;
GO
IF COL_LENGTH('log.sharepoint_ingestion_audit', 'destination_database') IS NULL
    ALTER TABLE log.sharepoint_ingestion_audit ADD destination_database VARCHAR(128) NULL;
GO
IF COL_LENGTH('log.sharepoint_ingestion_audit', 'destination_table') IS NULL
    ALTER TABLE log.sharepoint_ingestion_audit ADD destination_table VARCHAR(300) NULL;
GO

-- Drop retired columns
IF COL_LENGTH('log.sharepoint_ingestion_audit', 'ingestion_domain') IS NOT NULL
    ALTER TABLE log.sharepoint_ingestion_audit DROP COLUMN ingestion_domain;
GO
IF COL_LENGTH('log.sharepoint_ingestion_audit', 'batch_id') IS NOT NULL
    ALTER TABLE log.sharepoint_ingestion_audit DROP COLUMN batch_id;
GO

-- STG DB (dev)
USE ingest_stg_dev;
GO
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'sharepoint')
    EXEC('CREATE SCHEMA [sharepoint] AUTHORIZATION [dbo]');
GO
-- Drop legacy staging schema objects if they still exist (one-time cleanup)
IF OBJECT_ID('staging.sample_ingestion_target', 'U') IS NOT NULL DROP TABLE staging.sample_ingestion_target;
GO
IF OBJECT_ID('sharepoint.sample_ingestion_target', 'U') IS NULL
BEGIN
    CREATE TABLE sharepoint.sample_ingestion_target (
        business_key           VARCHAR(50)    NOT NULL,
        name                   VARCHAR(200)   NULL,
        amount                 DECIMAL(18,2)  NULL,
        effective_date         DATE           NULL,
        source_file_name       VARCHAR(255)   NULL,
        sp_ingest_created_utc  DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME(),
        sp_ingest_modified_utc DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_stg_dev_sample_ingestion_target PRIMARY KEY (business_key)
    );
END
GO

-- INT DB (dev)
USE ingest_int_dev;
GO
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'sharepoint')
    EXEC('CREATE SCHEMA [sharepoint] AUTHORIZATION [dbo]');
GO
-- Drop legacy staging schema objects if they still exist (one-time cleanup)
IF OBJECT_ID('staging.sample_ingestion_target', 'U') IS NOT NULL DROP TABLE staging.sample_ingestion_target;
GO
IF OBJECT_ID('sharepoint.sample_ingestion_target', 'U') IS NULL
BEGIN
    CREATE TABLE sharepoint.sample_ingestion_target (
        business_key           VARCHAR(50)    NOT NULL,
        name                   VARCHAR(200)   NULL,
        amount                 DECIMAL(18,2)  NULL,
        effective_date         DATE           NULL,
        source_file_name       VARCHAR(255)   NULL,
        sp_ingest_created_utc  DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME(),
        sp_ingest_modified_utc DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_int_dev_sample_ingestion_target PRIMARY KEY (business_key)
    );
END
GO

-- ===========================================================================
--  Section D  —  MIGRATION from legacy ingest_prod / ingest_dev
-- ===========================================================================
-- If the old databases still exist, run these steps ONCE to migrate objects.
-- Comment out after running.
-- ===========================================================================

/*
-- ── Migrate config + log from ingest_prod → ingest_audit_prod ──────────────
-- (Use SSMS Import/Export or INSERT INTO ... SELECT across linked servers.)
-- Example:
INSERT INTO ingest_audit_prod.config.sharepoint_ingestion (...)
SELECT ... FROM ingest_prod.dbo.config_sharepoint_ingestion ...

-- ── Migrate data tables from ingest_prod sharepoint.* → ingest_int_prod sharepoint.* ──
-- INSERT INTO ingest_int_prod.sharepoint.my_table SELECT * FROM ingest_prod.sharepoint.my_table

-- ── Rename legacy DBs when migration is verified ───────────────────────────
-- ALTER DATABASE ingest_prod  MODIFY NAME = ingest_prod_legacy;
-- ALTER DATABASE ingest_dev   MODIFY NAME = ingest_dev_legacy;
*/
