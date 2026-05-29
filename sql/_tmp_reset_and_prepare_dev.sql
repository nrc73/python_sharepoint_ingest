USE ingest_dev;
GO

-- Clean up legacy destination artifacts created under dbo schema.
IF OBJECT_ID('dbo.dest_customers', 'U') IS NOT NULL DROP TABLE dbo.dest_customers;
GO
IF OBJECT_ID('dbo.dest_transactions', 'U') IS NOT NULL DROP TABLE dbo.dest_transactions;
GO
IF OBJECT_ID('dbo.dest_transactions_parquet', 'U') IS NOT NULL DROP TABLE dbo.dest_transactions_parquet;
GO
IF OBJECT_ID('dbo.dest_transactions_large', 'U') IS NOT NULL DROP TABLE dbo.dest_transactions_large;
GO
IF OBJECT_ID('dbo.dest_invalid_csv', 'U') IS NOT NULL DROP TABLE dbo.dest_invalid_csv;
GO
IF OBJECT_ID('dbo.dest_invalid_excel', 'U') IS NOT NULL DROP TABLE dbo.dest_invalid_excel;
GO
IF OBJECT_ID('dbo.dest_invalid_parquet', 'U') IS NOT NULL DROP TABLE dbo.dest_invalid_parquet;
GO
IF OBJECT_ID('dbo.sample_ingestion_target', 'U') IS NOT NULL DROP TABLE dbo.sample_ingestion_target;
GO

DECLARE @drop_sql NVARCHAR(MAX) = N'';
SELECT @drop_sql = @drop_sql + N'DROP TABLE ' + QUOTENAME(SCHEMA_NAME(schema_id)) + N'.' + QUOTENAME(name) + N';'
FROM sys.tables
WHERE name LIKE '[_]tmp[_]%';

IF (@drop_sql <> N'')
BEGIN
    EXEC sp_executesql @drop_sql;
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

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'destination_database') IS NULL
BEGIN
    ALTER TABLE log.sharepoint_ingestion_audit ADD destination_database VARCHAR(128) NULL;
END
GO

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'destination_table') IS NULL
BEGIN
    ALTER TABLE log.sharepoint_ingestion_audit ADD destination_table VARCHAR(300) NULL;
END
GO

DELETE FROM log.sharepoint_ingestion_audit;
GO

DELETE FROM config.sharepoint_ingestion;
GO

IF OBJECT_ID('sharepoint.dest_customers', 'U') IS NOT NULL
    TRUNCATE TABLE sharepoint.dest_customers;
GO

IF OBJECT_ID('sharepoint.dest_transactions', 'U') IS NOT NULL
    TRUNCATE TABLE sharepoint.dest_transactions;
GO

IF OBJECT_ID('sharepoint.dest_transactions_parquet', 'U') IS NOT NULL
    TRUNCATE TABLE sharepoint.dest_transactions_parquet;
GO

IF OBJECT_ID('sharepoint.dest_transactions_large', 'U') IS NOT NULL
    TRUNCATE TABLE sharepoint.dest_transactions_large;
GO

IF OBJECT_ID('sharepoint.dest_invalid_csv', 'U') IS NOT NULL
    TRUNCATE TABLE sharepoint.dest_invalid_csv;
GO

IF OBJECT_ID('sharepoint.dest_invalid_excel', 'U') IS NOT NULL
    TRUNCATE TABLE sharepoint.dest_invalid_excel;
GO

IF OBJECT_ID('sharepoint.dest_invalid_parquet', 'U') IS NOT NULL
    TRUNCATE TABLE sharepoint.dest_invalid_parquet;
GO

IF OBJECT_ID('sharepoint.sample_ingestion_target', 'U') IS NOT NULL
    TRUNCATE TABLE sharepoint.sample_ingestion_target;
GO










