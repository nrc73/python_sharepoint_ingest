USE ingest_dev;
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

DELETE FROM log.sharepoint_ingestion_audit;
GO

DELETE FROM config.sharepoint_ingestion;
GO

TRUNCATE TABLE sharepoint.dest_customers;
GO

TRUNCATE TABLE sharepoint.dest_transactions;
GO

TRUNCATE TABLE sharepoint.dest_transactions_large;
GO

TRUNCATE TABLE sharepoint.dest_invalid_csv;
GO

TRUNCATE TABLE sharepoint.dest_invalid_excel;
GO

TRUNCATE TABLE sharepoint.sample_ingestion_target;
GO


