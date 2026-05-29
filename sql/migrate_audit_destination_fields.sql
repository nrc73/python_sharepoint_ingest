-- =============================================================================
-- migrate_audit_destination_fields.sql
-- =============================================================================
-- Adds destination_database and destination_table columns to
-- log.sharepoint_ingestion_audit.
--
-- Run this script once against EACH audit database that is missing the columns:
--
--   sqlcmd -S <server> -d ingest_audit_dev  -i migrate_audit_destination_fields.sql
--   sqlcmd -S <server> -d ingest_audit_prod -i migrate_audit_destination_fields.sql
--   sqlcmd -S <server> -d ingest_dev        -i migrate_audit_destination_fields.sql  (legacy)
--
-- The script is fully idempotent — safe to run multiple times.
-- =============================================================================

PRINT 'migrate_audit_destination_fields.sql — running against ' + DB_NAME();
GO

IF OBJECT_ID('log.sharepoint_ingestion_audit', 'U') IS NULL
BEGIN
    PRINT 'WARNING: log.sharepoint_ingestion_audit does not exist in this database — skipping.';
    RETURN;
END
GO

-- ── destination_database ──────────────────────────────────────────────────────

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'destination_database') IS NULL
BEGIN
    ALTER TABLE log.sharepoint_ingestion_audit
        ADD destination_database VARCHAR(128) NULL;
    PRINT 'Added: log.sharepoint_ingestion_audit.destination_database';
END
ELSE
    PRINT 'Already present: log.sharepoint_ingestion_audit.destination_database';
GO

-- ── destination_table ─────────────────────────────────────────────────────────

IF COL_LENGTH('log.sharepoint_ingestion_audit', 'destination_table') IS NULL
BEGIN
    ALTER TABLE log.sharepoint_ingestion_audit
        ADD destination_table VARCHAR(300) NULL;
    PRINT 'Added: log.sharepoint_ingestion_audit.destination_table';
END
ELSE
    PRINT 'Already present: log.sharepoint_ingestion_audit.destination_table';
GO

PRINT 'migrate_audit_destination_fields.sql — complete on ' + DB_NAME();
GO
