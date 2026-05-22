IF DB_ID('ingest_prod') IS NULL
BEGIN
    CREATE DATABASE ingest_prod;
END
GO

USE ingest_prod;
GO

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'config')
BEGIN
    EXEC('CREATE SCHEMA [config] AUTHORIZATION [dbo]');
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'log')
BEGIN
    EXEC('CREATE SCHEMA [log] AUTHORIZATION [dbo]');
END
GO

IF OBJECT_ID('config.sharepoint_ingestion', 'U') IS NULL
BEGIN
    CREATE TABLE config.sharepoint_ingestion (
        id INT IDENTITY(1,1) PRIMARY KEY,
        sharepoint_base_url VARCHAR(500) NOT NULL,
        sharepoint_process_folder VARCHAR(200) NOT NULL,
        excel_tab_name VARCHAR(100) NOT NULL,
        sharepoint_process_archive_folder VARCHAR(200),
        sharepoint_process_failed_folder VARCHAR(200),
        process_frequency VARCHAR(50),
        header_skip_rows INT DEFAULT 0,
        check_source_dest_columns varchar(1),
        multi_file_ingest varchar(1),
        error_notification_email_address VARCHAR(200) DEFAULT 'NathanChapman@company715.onmicrosoft.com',
        process_id UNIQUEIDENTIFIER,
        workflow_id VARCHAR(100),
        staging_table_name VARCHAR(200) NOT NULL,
        is_active varchar(1) DEFAULT 1,
        file_name_pattern VARCHAR(255) NULL,
        load_strategy VARCHAR(30) NULL,
        merge_key_columns VARCHAR(400) NULL,
        column_mapping_json VARCHAR(MAX) NULL,
        sp_ingest_created_utc DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        sp_ingest_modified_utc DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
    );
END
GO

IF COL_LENGTH('config.sharepoint_ingestion', 'sp_ingest_created_utc') IS NULL
    AND COL_LENGTH('config.sharepoint_ingestion', 'created_date') IS NOT NULL
BEGIN
    EXEC sp_rename 'config.sharepoint_ingestion.created_date', 'sp_ingest_created_utc', 'COLUMN';
END
GO

IF COL_LENGTH('config.sharepoint_ingestion', 'sp_ingest_modified_utc') IS NULL
    AND COL_LENGTH('config.sharepoint_ingestion', 'modified_date') IS NOT NULL
BEGIN
    EXEC sp_rename 'config.sharepoint_ingestion.modified_date', 'sp_ingest_modified_utc', 'COLUMN';
END
GO

IF COL_LENGTH('config.sharepoint_ingestion', 'ingestion_scope') IS NULL
BEGIN
    ALTER TABLE config.sharepoint_ingestion ADD ingestion_scope VARCHAR(20) NOT NULL CONSTRAINT DF_sharepoint_ingestion_scope DEFAULT 'REAL';
END
GO

IF COL_LENGTH('config.sharepoint_ingestion', 'ingestion_domain') IS NULL
BEGIN
    ALTER TABLE config.sharepoint_ingestion ADD ingestion_domain VARCHAR(50) NULL;
END
GO

IF COL_LENGTH('config.sharepoint_ingestion', 'is_test_data') IS NULL
BEGIN
    ALTER TABLE config.sharepoint_ingestion ADD is_test_data BIT NOT NULL CONSTRAINT DF_sharepoint_ingestion_is_test_data DEFAULT 0;
END
GO

IF COLUMNPROPERTY(OBJECT_ID('config.sharepoint_ingestion'), 'is_validated', 'ColumnId') IS NULL
    ALTER TABLE config.sharepoint_ingestion ADD is_validated BIT NOT NULL DEFAULT 1;
GO

UPDATE config.sharepoint_ingestion
SET ingestion_scope = CASE
        WHEN (is_test_data = 1 OR is_test_data = '1' OR is_test_data = 'Y' OR is_test_data = 'y') THEN 'TEST'
        ELSE 'REAL'
    END
WHERE ingestion_scope IS NULL OR LTRIM(RTRIM(ingestion_scope)) = '';
GO

IF OBJECT_ID('log.sharepoint_ingestion_audit', 'U') IS NULL
BEGIN
    CREATE TABLE log.sharepoint_ingestion_audit (
        audit_id BIGINT IDENTITY(1,1) PRIMARY KEY,
        config_id INT NOT NULL,
        workflow_id VARCHAR(100) NULL,
        process_id UNIQUEIDENTIFIER NULL,
        file_name VARCHAR(255) NULL,
        status VARCHAR(20) NOT NULL,
        records_loaded INT NULL,
        batch_id UNIQUEIDENTIFIER NULL,
        rows_scanned INT NULL,
        validation_error_count INT NULL,
        memory_peak_mb DECIMAL(18,2) NULL,
        duration_seconds DECIMAL(18,2) NULL,
        message VARCHAR(MAX) NULL,
        sp_ingest_created_utc DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
    );
END
GO

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'sp_ingest_created_utc') IS NULL
    AND COL_LENGTH('log.sharepoint_ingestion_audit', 'created_date') IS NOT NULL
BEGIN
    EXEC sp_rename 'log.sharepoint_ingestion_audit.created_date', 'sp_ingest_created_utc', 'COLUMN';
END
GO

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'batch_id') IS NULL
BEGIN
    ALTER TABLE log.sharepoint_ingestion_audit ADD batch_id UNIQUEIDENTIFIER NULL;
END
GO

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'rows_scanned') IS NULL
BEGIN
    ALTER TABLE log.sharepoint_ingestion_audit ADD rows_scanned INT NULL;
END
GO

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'validation_error_count') IS NULL
BEGIN
    ALTER TABLE log.sharepoint_ingestion_audit ADD validation_error_count INT NULL;
END
GO

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'memory_peak_mb') IS NULL
BEGIN
    ALTER TABLE log.sharepoint_ingestion_audit ADD memory_peak_mb DECIMAL(18,2) NULL;
END
GO

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'duration_seconds') IS NULL
BEGIN
    ALTER TABLE log.sharepoint_ingestion_audit ADD duration_seconds DECIMAL(18,2) NULL;
END
GO

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'ingestion_scope') IS NULL
BEGIN
    ALTER TABLE log.sharepoint_ingestion_audit ADD ingestion_scope VARCHAR(20) NULL;
END
GO

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'ingestion_domain') IS NULL
BEGIN
    ALTER TABLE log.sharepoint_ingestion_audit ADD ingestion_domain VARCHAR(50) NULL;
END
GO

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'is_test_data') IS NULL
BEGIN
    ALTER TABLE log.sharepoint_ingestion_audit ADD is_test_data BIT NULL;
END
GO

IF COLUMNPROPERTY(OBJECT_ID('log.sharepoint_ingestion_audit'), 'is_validated', 'ColumnId') IS NULL
    ALTER TABLE log.sharepoint_ingestion_audit ADD is_validated BIT NULL;
GO

IF OBJECT_ID('dbo.sample_ingestion_target', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.sample_ingestion_target (
        business_key VARCHAR(50) NOT NULL,
        name VARCHAR(200) NULL,
        amount DECIMAL(18,2) NULL,
        effective_date DATE NULL,
        source_file_name VARCHAR(255) NULL,
        sp_ingest_created_utc DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        sp_ingest_modified_utc DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_sample_ingestion_target PRIMARY KEY (business_key)
    );
END
GO

IF COL_LENGTH('dbo.sample_ingestion_target', 'sp_ingest_created_utc') IS NULL
    AND COL_LENGTH('dbo.sample_ingestion_target', 'created_date') IS NOT NULL
BEGIN
    EXEC sp_rename 'dbo.sample_ingestion_target.created_date', 'sp_ingest_created_utc', 'COLUMN';
END
GO

IF COL_LENGTH('dbo.sample_ingestion_target', 'sp_ingest_modified_utc') IS NULL
    AND COL_LENGTH('dbo.sample_ingestion_target', 'modified_date') IS NOT NULL
BEGIN
    EXEC sp_rename 'dbo.sample_ingestion_target.modified_date', 'sp_ingest_modified_utc', 'COLUMN';
END
GO

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
        error_notification_email_address,
        process_id,
        workflow_id,
        staging_table_name,
        is_active,
        ingestion_scope,
        ingestion_domain,
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
        NEWID(),
        'workflow-sample-001',
        'dbo.sample_ingestion_target',
        '1',
        'REAL',
        NULL,
        0,
        '*.csv',
        'TRUNCATE',
        'business_key',
        '{"BusinessKey":"business_key","Name":"name","Amount":"amount","EffectiveDate":"effective_date"}'
    );
END
GO
