IF DB_ID('ingest_dev') IS NULL
BEGIN
    CREATE DATABASE ingest_dev;
END
GO

USE ingest_dev;
GO

IF DB_NAME() <> 'ingest_dev'
BEGIN
    RAISERROR('Guard rail violation: sql/setup_ingest_dev_valid_artifacts.sql must be executed against ingest_dev only.', 16, 1);
    RETURN;
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'config')
BEGIN
    EXEC('CREATE SCHEMA [config] AUTHORIZATION [dbo]');
END
GO

IF OBJECT_ID('sharepoint.dest_customers', 'U') IS NULL
BEGIN
    CREATE TABLE sharepoint.dest_customers (
        customer_id VARCHAR(20) NOT NULL,
        customer_name VARCHAR(200) NULL,
        signup_date DATE NULL,
        credit_limit DECIMAL(18,2) NULL,
        is_active VARCHAR(1) NULL,
        region_code VARCHAR(10) NULL,
        source_system VARCHAR(50) NULL,
        excel_tab_name VARCHAR(100) NOT NULL,
        source_file_name VARCHAR(255) NULL,
        load_datetime DATETIME NOT NULL DEFAULT GETUTCDATE(),
        [__$batch_id] INT NULL,
        [__$job_instance_id] INT NULL,
        CONSTRAINT PK_dest_customers PRIMARY KEY (customer_id, excel_tab_name)
    );
END
GO

IF COL_LENGTH('sharepoint.dest_customers', 'load_datetime') IS NULL
    AND COL_LENGTH('sharepoint.dest_customers', 'sp_ingest_created_utc') IS NOT NULL
BEGIN
    EXEC sp_rename 'sharepoint.dest_customers.sp_ingest_created_utc', 'load_datetime', 'COLUMN';
END
GO

IF COL_LENGTH('sharepoint.dest_customers', '__$batch_id') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_customers ADD [__$batch_id] INT NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_customers', '__$job_instance_id') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_customers ADD [__$job_instance_id] INT NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_customers', 'sp_ingest_modified_utc') IS NOT NULL
BEGIN
    ALTER TABLE sharepoint.dest_customers DROP COLUMN sp_ingest_modified_utc;
END
GO

IF COL_LENGTH('sharepoint.dest_customers', 'excel_tab_name') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_customers ADD excel_tab_name VARCHAR(100) NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_customers', 'source_file_name') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_customers ADD source_file_name VARCHAR(255) NULL;
END
GO

IF OBJECT_ID('sharepoint.dest_transactions', 'U') IS NULL
BEGIN
    CREATE TABLE sharepoint.dest_transactions (
        transaction_id VARCHAR(20) NOT NULL,
        customer_id VARCHAR(20) NULL,
        transaction_date DATE NULL,
        amount DECIMAL(18,2) NULL,
        currency VARCHAR(10) NULL,
        status VARCHAR(20) NULL,
        source_file_name VARCHAR(255) NULL,
        load_datetime DATETIME NOT NULL DEFAULT GETUTCDATE(),
        [__$batch_id] INT NULL,
        [__$job_instance_id] INT NULL,
        CONSTRAINT PK_dest_transactions PRIMARY KEY (transaction_id)
    );
END
GO

IF COL_LENGTH('sharepoint.dest_transactions', 'load_datetime') IS NULL
    AND COL_LENGTH('sharepoint.dest_transactions', 'sp_ingest_created_utc') IS NOT NULL
BEGIN
    EXEC sp_rename 'sharepoint.dest_transactions.sp_ingest_created_utc', 'load_datetime', 'COLUMN';
END
GO

IF COL_LENGTH('sharepoint.dest_transactions', '__$batch_id') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions ADD [__$batch_id] INT NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_transactions', '__$job_instance_id') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions ADD [__$job_instance_id] INT NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_transactions', 'sp_ingest_modified_utc') IS NOT NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions DROP COLUMN sp_ingest_modified_utc;
END
GO

IF COL_LENGTH('sharepoint.dest_transactions', 'source_file_name') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions ADD source_file_name VARCHAR(255) NULL;
END
GO

IF OBJECT_ID('sharepoint.dest_transactions_parquet', 'U') IS NULL
BEGIN
    CREATE TABLE sharepoint.dest_transactions_parquet (
        transaction_id VARCHAR(20) NOT NULL,
        customer_id VARCHAR(20) NULL,
        transaction_date DATE NULL,
        amount DECIMAL(18,2) NULL,
        currency VARCHAR(10) NULL,
        status VARCHAR(20) NULL,
        source_system VARCHAR(50) NULL,
        source_file_name VARCHAR(255) NULL,
        load_datetime DATETIME NOT NULL DEFAULT GETUTCDATE(),
        [__$batch_id] INT NULL,
        [__$job_instance_id] INT NULL,
        CONSTRAINT PK_dest_transactions_parquet PRIMARY KEY (transaction_id)
    );
END
GO

IF COL_LENGTH('sharepoint.dest_transactions_parquet', 'source_file_name') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions_parquet ADD source_file_name VARCHAR(255) NULL;
END
GO

IF OBJECT_ID('sharepoint.dest_transactions_large', 'U') IS NULL
BEGIN
    CREATE TABLE sharepoint.dest_transactions_large (
        transaction_id VARCHAR(20) NOT NULL,
        customer_id VARCHAR(20) NULL,
        transaction_date DATE NULL,
        amount DECIMAL(18,2) NULL,
        currency VARCHAR(10) NULL,
        status VARCHAR(20) NULL,
        quantity INT NULL,
        discount_rate DECIMAL(9,4) NULL,
        fee_amount DECIMAL(18,2) NULL,
        tax_amount DECIMAL(18,2) NULL,
        net_amount DECIMAL(18,2) NULL,
        channel VARCHAR(20) NULL,
        region VARCHAR(10) NULL,
        source_system VARCHAR(50) NULL,
        batch_id VARCHAR(50) NULL,
        event_timestamp DATETIME2 NULL,
        is_priority VARCHAR(1) NULL,
        reference_code VARCHAR(50) NULL,
        ledger_code VARCHAR(20) NULL,
        comment_text VARCHAR(500) NULL,
        source_file_name VARCHAR(255) NULL,
        load_datetime DATETIME NOT NULL DEFAULT GETUTCDATE(),
        [__$batch_id] INT NULL,
        [__$job_instance_id] INT NULL,
        CONSTRAINT PK_dest_transactions_large PRIMARY KEY (transaction_id)
    );
END
GO

IF COL_LENGTH('sharepoint.dest_transactions_parquet', 'load_datetime') IS NULL
    AND COL_LENGTH('sharepoint.dest_transactions_parquet', 'sp_ingest_created_utc') IS NOT NULL
BEGIN
    EXEC sp_rename 'sharepoint.dest_transactions_parquet.sp_ingest_created_utc', 'load_datetime', 'COLUMN';
END
GO

IF COL_LENGTH('sharepoint.dest_transactions_parquet', '__$batch_id') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions_parquet ADD [__$batch_id] INT NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_transactions_parquet', '__$job_instance_id') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions_parquet ADD [__$job_instance_id] INT NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_transactions_parquet', 'sp_ingest_modified_utc') IS NOT NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions_parquet DROP COLUMN sp_ingest_modified_utc;
END
GO

IF COL_LENGTH('sharepoint.dest_transactions_large', 'load_datetime') IS NULL
    AND COL_LENGTH('sharepoint.dest_transactions_large', 'sp_ingest_created_utc') IS NOT NULL
BEGIN
    EXEC sp_rename 'sharepoint.dest_transactions_large.sp_ingest_created_utc', 'load_datetime', 'COLUMN';
END
GO

IF COL_LENGTH('sharepoint.dest_transactions_large', '__$batch_id') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions_large ADD [__$batch_id] INT NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_transactions_large', '__$job_instance_id') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions_large ADD [__$job_instance_id] INT NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_transactions_large', 'sp_ingest_modified_utc') IS NOT NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions_large DROP COLUMN sp_ingest_modified_utc;
END
GO

IF COL_LENGTH('config.sharepoint_ingestion', 'sp_ingest_modified_utc') IS NULL
    AND COL_LENGTH('config.sharepoint_ingestion', 'modified_date') IS NOT NULL
BEGIN
    EXEC sp_rename 'config.sharepoint_ingestion.modified_date', 'sp_ingest_modified_utc', 'COLUMN';
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

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'is_validated') IS NULL
BEGIN
    ALTER TABLE log.sharepoint_ingestion_audit ADD is_validated BIT NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_transactions_large', 'source_file_name') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions_large ADD source_file_name VARCHAR(255) NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_transactions_large', 'quantity') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions_large ADD quantity INT NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_transactions_large', 'discount_rate') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions_large ADD discount_rate DECIMAL(9,4) NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_transactions_large', 'fee_amount') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions_large ADD fee_amount DECIMAL(18,2) NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_transactions_large', 'tax_amount') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions_large ADD tax_amount DECIMAL(18,2) NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_transactions_large', 'net_amount') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions_large ADD net_amount DECIMAL(18,2) NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_transactions_large', 'channel') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions_large ADD channel VARCHAR(20) NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_transactions_large', 'region') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions_large ADD region VARCHAR(10) NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_transactions_large', 'source_system') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions_large ADD source_system VARCHAR(50) NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_transactions_large', 'batch_id') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions_large ADD batch_id VARCHAR(50) NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_transactions_large', 'event_timestamp') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions_large ADD event_timestamp DATETIME2 NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_transactions_large', 'is_priority') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions_large ADD is_priority VARCHAR(1) NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_transactions_large', 'reference_code') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions_large ADD reference_code VARCHAR(50) NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_transactions_large', 'ledger_code') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions_large ADD ledger_code VARCHAR(20) NULL;
END
GO

IF COL_LENGTH('sharepoint.dest_transactions_large', 'comment_text') IS NULL
BEGIN
    ALTER TABLE sharepoint.dest_transactions_large ADD comment_text VARCHAR(500) NULL;
END
GO

UPDATE config.sharepoint_ingestion
SET is_active = '0',
    sp_ingest_modified_utc = SYSUTCDATETIME()
WHERE workflow_id IN ('wf-valid-customers-au', 'wf-valid-customers-us');
GO

MERGE config.sharepoint_ingestion AS target
USING (SELECT 'wf-valid-customers' AS workflow_id) AS source
ON target.workflow_id = source.workflow_id
WHEN MATCHED THEN
    UPDATE SET
        sharepoint_base_url = '{env:sharepoint_site_url}',
        sharepoint_process_folder = '/Documents/valid_customers',
        excel_tab_name = 'ALL_SHEETS',
        sharepoint_process_archive_folder = '/Documents/valid_customers/Processed',
        sharepoint_process_failed_folder = '/Documents/valid_customers/Failed',
        process_frequency = 'OnDemand',
        header_skip_rows = 0,
        check_source_dest_columns = '1',
        multi_file_ingest = '1',
        error_notification_email_address = 'NathanChapman@company715.onmicrosoft.com',
        process_id = COALESCE(target.process_id, NEWID()),
        staging_table_name = 'sharepoint.dest_customers',
        is_active = '1',
        ingestion_scope = 'TEST',
        ingestion_domain = 'sample_artifacts',
        is_test_data = 1,
        file_name_pattern = 'valid_customers_*.xlsx',
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
        '/Documents/valid_customers',
        'ALL_SHEETS',
        '/Documents/valid_customers/Processed',
        '/Documents/valid_customers/Failed',
        'OnDemand',
        0,
        '1',
        '1',
        'NathanChapman@company715.onmicrosoft.com',
        NEWID(),
        'wf-valid-customers',
        'sharepoint.dest_customers',
        '1',
        'TEST',
        'sample_artifacts',
        1,
        'valid_customers_*.xlsx',
        'APPEND',
        'customer_id,excel_tab_name',
        '{"CustomerId":"customer_id","CustomerName":"customer_name","SignupDate":"signup_date","CreditLimit":"credit_limit","IsActive":"is_active","RegionCode":"region_code","SourceSystem":"source_system"}'
    );
GO

MERGE config.sharepoint_ingestion AS target
USING (SELECT 'wf-valid-transactions-standard' AS workflow_id) AS source
ON target.workflow_id = source.workflow_id
WHEN MATCHED THEN
    UPDATE SET
        sharepoint_base_url = '{env:sharepoint_site_url}',
        sharepoint_process_folder = '/Documents/valid_transactions',
        excel_tab_name = '',
        sharepoint_process_archive_folder = '/Documents/valid_transactions/Processed',
        sharepoint_process_failed_folder = '/Documents/valid_transactions/Failed',
        process_frequency = 'OnDemand',
        header_skip_rows = 0,
        check_source_dest_columns = '1',
        multi_file_ingest = '1',
        error_notification_email_address = 'NathanChapman@company715.onmicrosoft.com',
        process_id = COALESCE(target.process_id, NEWID()),
        staging_table_name = 'sharepoint.dest_transactions',
        is_active = '1',
        ingestion_scope = 'TEST',
        ingestion_domain = 'sample_artifacts',
        is_test_data = 1,
        file_name_pattern = 'valid_transactions_00[12].csv',
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
        '/Documents/valid_transactions',
        '',
        '/Documents/valid_transactions/Processed',
        '/Documents/valid_transactions/Failed',
        'OnDemand',
        0,
        '1',
        '1',
        'NathanChapman@company715.onmicrosoft.com',
        NEWID(),
        'wf-valid-transactions-standard',
        'sharepoint.dest_transactions',
        '1',
        'TEST',
        'sample_artifacts',
        1,
        'valid_transactions_00[12].csv',
        'APPEND',
        'transaction_id',
        '{"TransactionId":"transaction_id","CustomerId":"customer_id","TransactionDate":"transaction_date","Amount":"amount","Currency":"currency","Status":"status"}'
    );
GO

MERGE config.sharepoint_ingestion AS target
USING (SELECT 'wf-valid-transactions-parquet' AS workflow_id) AS source
ON target.workflow_id = source.workflow_id
WHEN MATCHED THEN
    UPDATE SET
        sharepoint_base_url = '{env:sharepoint_site_url}',
        sharepoint_process_folder = '/Documents/valid_parquet',
        excel_tab_name = '',
        sharepoint_process_archive_folder = '/Documents/valid_parquet/Processed',
        sharepoint_process_failed_folder = '/Documents/valid_parquet/Failed',
        process_frequency = 'OnDemand',
        header_skip_rows = 0,
        check_source_dest_columns = '1',
        multi_file_ingest = '1',
        error_notification_email_address = 'NathanChapman@company715.onmicrosoft.com',
        process_id = COALESCE(target.process_id, NEWID()),
        staging_table_name = 'sharepoint.dest_transactions_parquet',
        is_active = '1',
        ingestion_scope = 'TEST',
        ingestion_domain = 'sample_artifacts',
        is_test_data = 1,
        file_name_pattern = 'valid_transactions_parquet_*.parquet',
        load_strategy = 'APPEND',
        merge_key_columns = 'transaction_id',
        column_mapping_json = '{"TransactionId":"transaction_id","CustomerId":"customer_id","TransactionDate":"transaction_date","Amount":"amount","Currency":"currency","Status":"status","SourceSystem":"source_system"}',
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
        '/Documents/valid_parquet',
        '',
        '/Documents/valid_parquet/Processed',
        '/Documents/valid_parquet/Failed',
        'OnDemand',
        0,
        '1',
        '1',
        'NathanChapman@company715.onmicrosoft.com',
        NEWID(),
        'wf-valid-transactions-parquet',
        'sharepoint.dest_transactions_parquet',
        '1',
        'TEST',
        'sample_artifacts',
        1,
        'valid_transactions_parquet_*.parquet',
        'APPEND',
        'transaction_id',
        '{"TransactionId":"transaction_id","CustomerId":"customer_id","TransactionDate":"transaction_date","Amount":"amount","Currency":"currency","Status":"status","SourceSystem":"source_system"}'
    );
GO

MERGE config.sharepoint_ingestion AS target
USING (SELECT 'wf-valid-transactions-large' AS workflow_id) AS source
ON target.workflow_id = source.workflow_id
WHEN MATCHED THEN
    UPDATE SET
        sharepoint_base_url = '{env:sharepoint_site_url}',
        sharepoint_process_folder = '/Documents/valid_transactions_large',
        excel_tab_name = '',
        sharepoint_process_archive_folder = '/Documents/valid_transactions_large/Processed',
        sharepoint_process_failed_folder = '/Documents/valid_transactions_large/Failed',
        process_frequency = 'OnDemand',
        header_skip_rows = 2,
        check_source_dest_columns = '1',
        multi_file_ingest = '0',
        error_notification_email_address = 'NathanChapman@company715.onmicrosoft.com',
        process_id = COALESCE(target.process_id, NEWID()),
        staging_table_name = 'sharepoint.dest_transactions_large',
        is_active = '1',
        ingestion_scope = 'TEST',
        ingestion_domain = 'sample_artifacts',
        is_test_data = 1,
        file_name_pattern = 'valid_transactions_large.csv',
        load_strategy = 'TRUNCATE',
        merge_key_columns = 'transaction_id',
        column_mapping_json = '{"TransactionId":"transaction_id","CustomerId":"customer_id","TransactionDate":"transaction_date","Amount":"amount","Currency":"currency","Status":"status","Quantity":"quantity","DiscountRate":"discount_rate","FeeAmount":"fee_amount","TaxAmount":"tax_amount","NetAmount":"net_amount","Channel":"channel","Region":"region","SourceSystem":"source_system","BatchId":"batch_id","EventTimestamp":"event_timestamp","IsPriority":"is_priority","ReferenceCode":"reference_code","LedgerCode":"ledger_code","CommentText":"comment_text"}',
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
        '/Documents/valid_transactions_large',
        '',
        '/Documents/valid_transactions_large/Processed',
        '/Documents/valid_transactions_large/Failed',
        'OnDemand',
        2,
        '1',
        '0',
        'NathanChapman@company715.onmicrosoft.com',
        NEWID(),
        'wf-valid-transactions-large',
        'sharepoint.dest_transactions_large',
        '1',
        'TEST',
        'sample_artifacts',
        1,
        'valid_transactions_large.csv',
        'TRUNCATE',
        'transaction_id',
        '{"TransactionId":"transaction_id","CustomerId":"customer_id","TransactionDate":"transaction_date","Amount":"amount","Currency":"currency","Status":"status","Quantity":"quantity","DiscountRate":"discount_rate","FeeAmount":"fee_amount","TaxAmount":"tax_amount","NetAmount":"net_amount","Channel":"channel","Region":"region","SourceSystem":"source_system","BatchId":"batch_id","EventTimestamp":"event_timestamp","IsPriority":"is_priority","ReferenceCode":"reference_code","LedgerCode":"ledger_code","CommentText":"comment_text"}'
    );
GO



