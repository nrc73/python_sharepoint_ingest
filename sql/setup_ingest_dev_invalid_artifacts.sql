IF DB_ID('ingest_dev') IS NULL
BEGIN
    CREATE DATABASE ingest_dev;
END
GO

USE ingest_dev;
GO

IF DB_NAME() <> 'ingest_dev'
BEGIN
    RAISERROR('Guard rail violation: sql/setup_ingest_dev_invalid_artifacts.sql must be executed against ingest_dev only.', 16, 1);
    RETURN;
END
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

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'sharepoint')
BEGIN
    EXEC('CREATE SCHEMA [sharepoint] AUTHORIZATION [dbo]');
END
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
        ingestion_scope VARCHAR(20) NULL,
        ingestion_domain VARCHAR(50) NULL,
        is_test_data BIT NULL,
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

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'batch_id') IS NOT NULL
BEGIN
    ALTER TABLE log.sharepoint_ingestion_audit DROP COLUMN batch_id;
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

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'is_validated') IS NULL
BEGIN
    ALTER TABLE log.sharepoint_ingestion_audit ADD is_validated BIT NULL;
END
GO

IF COL_LENGTH('config.sharepoint_ingestion', 'is_validated') IS NULL
BEGIN
    ALTER TABLE config.sharepoint_ingestion ADD is_validated BIT NOT NULL CONSTRAINT DF_sharepoint_ingestion_is_validated DEFAULT 1;
END
GO

IF COL_LENGTH('config.sharepoint_ingestion', 'error_notification_cc_email_address') IS NULL
BEGIN
    ALTER TABLE config.sharepoint_ingestion ADD error_notification_cc_email_address VARCHAR(400) NULL;
END
GO

IF COL_LENGTH('config.sharepoint_ingestion', 'to_email_address') IS NULL
BEGIN
    ALTER TABLE config.sharepoint_ingestion ADD to_email_address VARCHAR(400) NULL;
END
GO

IF COL_LENGTH('config.sharepoint_ingestion', 'cc_email_address') IS NULL
BEGIN
    ALTER TABLE config.sharepoint_ingestion ADD cc_email_address VARCHAR(400) NULL;
END
GO

IF OBJECT_ID('sharepoint.dest_invalid_csv', 'U') IS NULL
BEGIN
    CREATE TABLE sharepoint.dest_invalid_csv (
        transaction_id VARCHAR(20) NOT NULL,
        customer_id VARCHAR(20) NULL,
        transaction_date DATE NULL,
        amount DECIMAL(18,2) NULL,
        currency VARCHAR(10) NULL,
        status VARCHAR(20) NULL,
        source_file_name VARCHAR(255) NULL,
        sp_ingest_load_dt DATETIME NOT NULL DEFAULT GETUTCDATE(),
        audit_id BIGINT NULL,
        [__$batch_id] INT NULL,
        [__$job_instance_id] INT NULL,
        CONSTRAINT PK_dest_invalid_csv PRIMARY KEY (transaction_id)
    );
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_csv', 'sp_ingest_load_dt') IS NULL
    AND COL_LENGTH('sharepoint.dest_invalid_csv', 'sp_ingest_created_utc') IS NOT NULL
BEGIN
    EXEC sp_rename 'sharepoint.dest_invalid_csv.sp_ingest_created_utc', 'sp_ingest_load_dt', 'COLUMN';
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_csv', 'audit_id') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_invalid_csv ADD audit_id BIGINT NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_csv', '__$batch_id') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_invalid_csv ADD [__$batch_id] INT NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_csv', '__$job_instance_id') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_invalid_csv ADD [__$job_instance_id] INT NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_csv', 'sp_ingest_modified_utc') IS NOT NULL
BEGIN
    ALTER TABLE sharepoint.dest_invalid_csv DROP COLUMN sp_ingest_modified_utc;
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_csv', 'source_file_name') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_invalid_csv ADD source_file_name VARCHAR(255) NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_csv', 'transaction_id') IS NULL
BEGIN
    IF COL_LENGTH('sharepoint.dest_invalid_csv', 'RecordId') IS NOT NULL
    BEGIN
        EXEC sp_rename 'sharepoint.dest_invalid_csv.RecordId', 'transaction_id', 'COLUMN';
    END
    ELSE
    BEGIN
        ALTER TABLE sharepoint.dest_invalid_csv ADD transaction_id VARCHAR(20) NULL;
    END
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_csv', 'customer_id') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_invalid_csv ADD customer_id VARCHAR(20) NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_csv', 'transaction_date') IS NULL
BEGIN
    IF COL_LENGTH('sharepoint.dest_invalid_csv', 'EffectiveDate') IS NOT NULL
    BEGIN
        EXEC sp_rename 'sharepoint.dest_invalid_csv.EffectiveDate', 'transaction_date', 'COLUMN';
    END
    ELSE
    BEGIN
        ALTER TABLE sharepoint.dest_invalid_csv ADD transaction_date DATE NULL;
    END
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_csv', 'amount') IS NULL
BEGIN
    IF COL_LENGTH('sharepoint.dest_invalid_csv', 'Amount') IS NOT NULL
    BEGIN
        EXEC sp_rename 'sharepoint.dest_invalid_csv.Amount', 'amount', 'COLUMN';
    END
    ELSE
    BEGIN
        ALTER TABLE sharepoint.dest_invalid_csv ADD amount DECIMAL(18,2) NULL;
    END
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_csv', 'currency') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_invalid_csv ADD currency VARCHAR(10) NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_csv', 'status') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_invalid_csv ADD status VARCHAR(20) NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_csv', 'Quantity') IS NOT NULL
BEGIN
    ALTER TABLE sharepoint.dest_invalid_csv DROP COLUMN Quantity;
END
GO

IF OBJECT_ID('sharepoint.dest_invalid_excel', 'U') IS NULL
BEGIN
    CREATE TABLE sharepoint.dest_invalid_excel (
        customer_id VARCHAR(20) NOT NULL,
        customer_name VARCHAR(200) NULL,
        signup_date DATE NULL,
        credit_limit DECIMAL(18,2) NULL,
        is_active VARCHAR(1) NULL,
        region_code VARCHAR(10) NULL,
        source_system VARCHAR(50) NULL,
        excel_tab_name VARCHAR(100) NOT NULL,
        source_file_name VARCHAR(255) NULL,
        sp_ingest_load_dt DATETIME NOT NULL DEFAULT GETUTCDATE(),
        audit_id BIGINT NULL,
        [__$batch_id] INT NULL,
        [__$job_instance_id] INT NULL,
        CONSTRAINT PK_dest_invalid_excel PRIMARY KEY (customer_id, excel_tab_name)
    );
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_excel', 'sp_ingest_load_dt') IS NULL
    AND COL_LENGTH('sharepoint.dest_invalid_excel', 'sp_ingest_created_utc') IS NOT NULL
BEGIN
    EXEC sp_rename 'sharepoint.dest_invalid_excel.sp_ingest_created_utc', 'sp_ingest_load_dt', 'COLUMN';
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_excel', 'audit_id') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_invalid_excel ADD audit_id BIGINT NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_excel', '__$batch_id') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_invalid_excel ADD [__$batch_id] INT NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_excel', '__$job_instance_id') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_invalid_excel ADD [__$job_instance_id] INT NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_excel', 'sp_ingest_modified_utc') IS NOT NULL
BEGIN
    ALTER TABLE sharepoint.dest_invalid_excel DROP COLUMN sp_ingest_modified_utc;
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_excel', 'source_file_name') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_invalid_excel ADD source_file_name VARCHAR(255) NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_excel', 'customer_id') IS NULL
BEGIN
    IF COL_LENGTH('sharepoint.dest_invalid_excel', 'CustomerId') IS NOT NULL
    BEGIN
        EXEC sp_rename 'sharepoint.dest_invalid_excel.CustomerId', 'customer_id', 'COLUMN';
    END
    ELSE
    BEGIN
        ALTER TABLE sharepoint.dest_invalid_excel ADD customer_id VARCHAR(20) NULL;
    END
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_excel', 'customer_name') IS NULL
BEGIN
    IF COL_LENGTH('sharepoint.dest_invalid_excel', 'CustomerName') IS NOT NULL
    BEGIN
        EXEC sp_rename 'sharepoint.dest_invalid_excel.CustomerName', 'customer_name', 'COLUMN';
    END
    ELSE
    BEGIN
        ALTER TABLE sharepoint.dest_invalid_excel ADD customer_name VARCHAR(200) NULL;
    END
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_excel', 'signup_date') IS NULL
BEGIN
    IF COL_LENGTH('sharepoint.dest_invalid_excel', 'SignupDate') IS NOT NULL
    BEGIN
        EXEC sp_rename 'sharepoint.dest_invalid_excel.SignupDate', 'signup_date', 'COLUMN';
    END
    ELSE
    BEGIN
        ALTER TABLE sharepoint.dest_invalid_excel ADD signup_date DATE NULL;
    END
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_excel', 'credit_limit') IS NULL
BEGIN
    IF COL_LENGTH('sharepoint.dest_invalid_excel', 'CreditLimit') IS NOT NULL
    BEGIN
        EXEC sp_rename 'sharepoint.dest_invalid_excel.CreditLimit', 'credit_limit', 'COLUMN';
    END
    ELSE
    BEGIN
        ALTER TABLE sharepoint.dest_invalid_excel ADD credit_limit DECIMAL(18,2) NULL;
    END
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_excel', 'is_active') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_invalid_excel ADD is_active VARCHAR(1) NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_excel', 'region_code') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_invalid_excel ADD region_code VARCHAR(10) NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_excel', 'source_system') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_invalid_excel ADD source_system VARCHAR(50) NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_excel', 'excel_tab_name') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_invalid_excel ADD excel_tab_name VARCHAR(100) NULL;
END
GO

IF OBJECT_ID('sharepoint.dest_invalid_parquet', 'U') IS NULL
BEGIN
    CREATE TABLE sharepoint.dest_invalid_parquet (
        transaction_id VARCHAR(20) NOT NULL,
        customer_id VARCHAR(20) NULL,
        transaction_date DATE NULL,
        amount DECIMAL(18,2) NULL,
        currency VARCHAR(10) NULL,
        status VARCHAR(10) NULL,
        source_system VARCHAR(50) NULL,
        source_file_name VARCHAR(255) NULL,
        sp_ingest_load_dt DATETIME NOT NULL DEFAULT GETUTCDATE(),
        audit_id BIGINT NULL,
        [__$batch_id] INT NULL,
        [__$job_instance_id] INT NULL,
        CONSTRAINT PK_dest_invalid_parquet PRIMARY KEY (transaction_id)
    );
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_parquet', 'audit_id') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_invalid_parquet ADD audit_id BIGINT NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_invalid_parquet', 'source_file_name') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_invalid_parquet ADD source_file_name VARCHAR(255) NULL;
END
GO

IF OBJECT_ID('config.sharepoint_ingestion', 'U') IS NULL
BEGIN
    RAISERROR('config.sharepoint_ingestion table does not exist. Run bootstrap first.', 16, 1);
    RETURN;
END
GO

IF COL_LENGTH('config.sharepoint_ingestion', 'sp_ingest_modified_utc') IS NULL
    AND COL_LENGTH('config.sharepoint_ingestion', 'modified_date') IS NOT NULL
BEGIN
    EXEC sp_rename 'config.sharepoint_ingestion.modified_date', 'sp_ingest_modified_utc', 'COLUMN';
END
GO

MERGE config.sharepoint_ingestion AS target
USING (SELECT 'wf-invalid-csv-all' AS workflow_id) AS source
ON target.workflow_id = source.workflow_id
WHEN MATCHED THEN
    UPDATE SET
        sharepoint_base_url = '{env:sharepoint_site_url}',
        sharepoint_process_folder = '/Documents/invalid_csv',
        excel_tab_name = '',
        sharepoint_process_archive_folder = '/Documents/invalid_csv/Processed',
        sharepoint_process_failed_folder = '/Documents/invalid_csv/Failed',
        process_frequency = 'OnDemand',
        header_skip_rows = 0,
        check_source_dest_columns = '1',
        multi_file_ingest = '1',
        error_notification_email_address = 'NathanChapman@company715.onmicrosoft.com',
        process_id = COALESCE(target.process_id, NEWID()),
        staging_table_name = 'sharepoint.dest_invalid_csv',
        is_active = '1',
        ingestion_scope = 'TEST',
        ingestion_domain = 'sample_artifacts',
        is_test_data = 1,
        file_name_pattern = 'invalid_*.csv',
        load_strategy = 'APPEND',
        merge_key_columns = 'transaction_id',
        column_mapping_json = '{"TransactionId":"transaction_id","CustomerId":"customer_id","TransactionDate":"transaction_date","Amount":"amount","Currency":"currency","Status":"status"}',
        sp_ingest_modified_utc = SYSUTCDATETIME()
WHEN NOT MATCHED THEN
    INSERT (
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
        '{env:sharepoint_site_url}',
        '/Documents/invalid_csv',
        '',
        '/Documents/invalid_csv/Processed',
        '/Documents/invalid_csv/Failed',
        'OnDemand',
        0,
        '1',
        '1',
        'NathanChapman@company715.onmicrosoft.com',
        NEWID(),
        'wf-invalid-csv-all',
        'sharepoint.dest_invalid_csv',
        '1',
        'TEST',
        'sample_artifacts',
        1,
        'invalid_*.csv',
        'APPEND',
        'transaction_id',
        '{"TransactionId":"transaction_id","CustomerId":"customer_id","TransactionDate":"transaction_date","Amount":"amount","Currency":"currency","Status":"status"}'
    );
GO

MERGE config.sharepoint_ingestion AS target
USING (SELECT 'wf-invalid-excel-all' AS workflow_id) AS source
ON target.workflow_id = source.workflow_id
WHEN MATCHED THEN
    UPDATE SET
        sharepoint_base_url = '{env:sharepoint_site_url}',
        sharepoint_process_folder = '/Documents/invalid_excel',
        excel_tab_name = 'ALL_SHEETS',
        sharepoint_process_archive_folder = '/Documents/invalid_excel/Processed',
        sharepoint_process_failed_folder = '/Documents/invalid_excel/Failed',
        process_frequency = 'OnDemand',
        header_skip_rows = 0,
        check_source_dest_columns = '1',
        multi_file_ingest = '1',
        error_notification_email_address = 'NathanChapman@company715.onmicrosoft.com',
        process_id = COALESCE(target.process_id, NEWID()),
        staging_table_name = 'sharepoint.dest_invalid_excel',
        is_active = '1',
        ingestion_scope = 'TEST',
        ingestion_domain = 'sample_artifacts',
        is_test_data = 1,
        file_name_pattern = 'invalid_(additional_unknown_columns|customers_multiple_datasets|datetime_stress|date_as_text|numeric_overflow).xlsx',
        load_strategy = 'APPEND',
        merge_key_columns = 'customer_id,excel_tab_name',
        column_mapping_json = '{"CustomerId":"customer_id","CustomerName":"customer_name","SignupDate":"signup_date","CreditLimit":"credit_limit","IsActive":"is_active","RegionCode":"region_code","SourceSystem":"source_system"}',
        sp_ingest_modified_utc = SYSUTCDATETIME()
WHEN NOT MATCHED THEN
    INSERT (
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
        '{env:sharepoint_site_url}',
        '/Documents/invalid_excel',
        'ALL_SHEETS',
        '/Documents/invalid_excel/Processed',
        '/Documents/invalid_excel/Failed',
        'OnDemand',
        0,
        '1',
        '1',
        'NathanChapman@company715.onmicrosoft.com',
        NEWID(),
        'wf-invalid-excel-all',
        'sharepoint.dest_invalid_excel',
        '1',
        'TEST',
        'sample_artifacts',
        1,
        'invalid_(additional_unknown_columns|customers_multiple_datasets|datetime_stress|date_as_text|numeric_overflow).xlsx',
        'APPEND',
        'customer_id,excel_tab_name',
        '{"CustomerId":"customer_id","CustomerName":"customer_name","SignupDate":"signup_date","CreditLimit":"credit_limit","IsActive":"is_active","RegionCode":"region_code","SourceSystem":"source_system"}'
    );
GO

MERGE config.sharepoint_ingestion AS target
USING (SELECT 'wf-invalid-excel-missing-tabs' AS workflow_id) AS source
ON target.workflow_id = source.workflow_id
WHEN MATCHED THEN
    UPDATE SET
        sharepoint_base_url = '{env:sharepoint_site_url}',
        sharepoint_process_folder = '/Documents/invalid_excel',
        excel_tab_name = 'Customers_AU',
        sharepoint_process_archive_folder = '/Documents/invalid_excel/Processed',
        sharepoint_process_failed_folder = '/Documents/invalid_excel/Failed',
        process_frequency = 'OnDemand',
        header_skip_rows = 0,
        check_source_dest_columns = '1',
        multi_file_ingest = '1',
        error_notification_email_address = 'NathanChapman@company715.onmicrosoft.com',
        process_id = COALESCE(target.process_id, NEWID()),
        staging_table_name = 'sharepoint.dest_invalid_excel',
        is_active = '1',
        ingestion_scope = 'TEST',
        ingestion_domain = 'sample_artifacts',
        is_test_data = 1,
        file_name_pattern = 'invalid_missing_tabs.xlsx',
        load_strategy = 'APPEND',
        merge_key_columns = 'customer_id,excel_tab_name',
        column_mapping_json = '{"CustomerId":"customer_id","CustomerName":"customer_name","SignupDate":"signup_date","CreditLimit":"credit_limit","IsActive":"is_active","RegionCode":"region_code","SourceSystem":"source_system"}',
        sp_ingest_modified_utc = SYSUTCDATETIME()
WHEN NOT MATCHED THEN
    INSERT (
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
        '{env:sharepoint_site_url}',
        '/Documents/invalid_excel',
        'Customers_AU',
        '/Documents/invalid_excel/Processed',
        '/Documents/invalid_excel/Failed',
        'OnDemand',
        0,
        '1',
        '1',
        'NathanChapman@company715.onmicrosoft.com',
        NEWID(),
        'wf-invalid-excel-missing-tabs',
        'sharepoint.dest_invalid_excel',
        '1',
        'TEST',
        'sample_artifacts',
        1,
        'invalid_missing_tabs.xlsx',
        'APPEND',
        'customer_id,excel_tab_name',
        '{"CustomerId":"customer_id","CustomerName":"customer_name","SignupDate":"signup_date","CreditLimit":"credit_limit","IsActive":"is_active","RegionCode":"region_code","SourceSystem":"source_system"}'
    );
GO

MERGE config.sharepoint_ingestion AS target
USING (SELECT 'wf-invalid-parquet-all' AS workflow_id) AS source
ON target.workflow_id = source.workflow_id
WHEN MATCHED THEN
    UPDATE SET
        sharepoint_base_url = '{env:sharepoint_site_url}',
        sharepoint_process_folder = '/Documents/invalid_parquet',
        excel_tab_name = '',
        sharepoint_process_archive_folder = '/Documents/invalid_parquet/Processed',
        sharepoint_process_failed_folder = '/Documents/invalid_parquet/Failed',
        process_frequency = 'OnDemand',
        header_skip_rows = 0,
        check_source_dest_columns = '1',
        multi_file_ingest = '1',
        error_notification_email_address = 'NathanChapman@company715.onmicrosoft.com',
        process_id = COALESCE(target.process_id, NEWID()),
        staging_table_name = 'sharepoint.dest_invalid_parquet',
        is_active = '1',
        ingestion_scope = 'TEST',
        ingestion_domain = 'sample_artifacts',
        is_test_data = 1,
        file_name_pattern = 'invalid_*parquet*.parquet',
        load_strategy = 'APPEND',
        merge_key_columns = 'transaction_id',
        column_mapping_json = NULL,
        sp_ingest_modified_utc = SYSUTCDATETIME()
WHEN NOT MATCHED THEN
    INSERT (
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
        '{env:sharepoint_site_url}',
        '/Documents/invalid_parquet',
        '',
        '/Documents/invalid_parquet/Processed',
        '/Documents/invalid_parquet/Failed',
        'OnDemand',
        0,
        '1',
        '1',
        'NathanChapman@company715.onmicrosoft.com',
        NEWID(),
        'wf-invalid-parquet-all',
        'sharepoint.dest_invalid_parquet',
        '1',
        'TEST',
        'sample_artifacts',
        1,
        'invalid_*parquet*.parquet',
        'APPEND',
        'transaction_id',
        NULL
    );
GO

UPDATE config.sharepoint_ingestion
SET
    to_email_address = COALESCE(NULLIF(LTRIM(RTRIM(to_email_address)), ''), error_notification_email_address),
    cc_email_address = COALESCE(NULLIF(LTRIM(RTRIM(cc_email_address)), ''), error_notification_cc_email_address),
    error_notification_email_address = COALESCE(NULLIF(LTRIM(RTRIM(error_notification_email_address)), ''), to_email_address),
    error_notification_cc_email_address = COALESCE(NULLIF(LTRIM(RTRIM(error_notification_cc_email_address)), ''), cc_email_address),
    sp_ingest_modified_utc = SYSUTCDATETIME()
WHERE workflow_id LIKE 'wf-invalid-%';
GO





