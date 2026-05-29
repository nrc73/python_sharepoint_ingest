-- =============================================================================
-- migrate_audit_reorder_destination_columns.sql
-- =============================================================================
-- Repositions [destination_database] and [destination_table] in
-- [log].[sharepoint_ingestion_audit] so they appear BEFORE [status].
--
-- SQL Server does not support ALTER TABLE ... MOVE COLUMN, so the only
-- approach is to recreate the table with the desired column order:
--
--   1. Create a shadow table with the target column order.
--   2. Copy all rows (including the IDENTITY value) from the old table.
--   3. Drop the old table.
--   4. Rename the shadow table to the canonical name.
--
-- SAFETY:
--   * Runs only against the database that contains the table.
--   * Guard-rail at the top rejects silent runs against wrong databases.
--   * Wrapped in an explicit transaction — rolls back automatically if any
--     step fails.
--   * Idempotent: skips the recreation if [destination_database] already sits
--     before [status] in the column ordinal list.
-- =============================================================================

USE ingest_audit_dev;
GO

IF DB_NAME() NOT IN ('ingest_audit_dev', 'ingest_audit_prd', 'ingest_audit_uat')
BEGIN
    RAISERROR('Guard: this migration must run against an ingest_audit_* database.', 16, 1);
    RETURN;
END
GO

-- ── Idempotency check ──────────────────────────────────────────────────────
-- If destination_database already has a lower ordinal position than status,
-- the table is already in the desired shape — nothing to do.
IF EXISTS (
    SELECT 1
    FROM   sys.columns c1
    JOIN   sys.columns c2
           ON  c1.object_id = c2.object_id
    WHERE  c1.object_id = OBJECT_ID('log.sharepoint_ingestion_audit')
      AND  c1.name      = 'destination_database'
      AND  c2.name      = 'status'
      AND  c1.column_id < c2.column_id   -- destination_database already before status
)
BEGIN
    PRINT 'Column order is already correct — migration skipped.';
    RETURN;
END
GO

-- ── Recreate with correct column order ────────────────────────────────────
BEGIN TRANSACTION;
BEGIN TRY

    -- Step 1: shadow table with the target column order
    CREATE TABLE log.sharepoint_ingestion_audit_reorder (
        audit_id               BIGINT IDENTITY(1,1) PRIMARY KEY,
        config_id              INT              NOT NULL,
        workflow_id            VARCHAR(100)     NULL,
        process_id             UNIQUEIDENTIFIER NULL,
        file_name              VARCHAR(255)     NULL,
        destination_database   VARCHAR(128)     NULL,   -- ← before status
        destination_table      VARCHAR(300)     NULL,   -- ← before status
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

    -- Step 2: copy all rows, preserving the IDENTITY value
    SET IDENTITY_INSERT log.sharepoint_ingestion_audit_reorder ON;

    INSERT INTO log.sharepoint_ingestion_audit_reorder (
        audit_id,
        config_id,
        workflow_id,
        process_id,
        file_name,
        destination_database,
        destination_table,
        status,
        records_loaded,
        rows_scanned,
        validation_error_count,
        memory_peak_mb,
        duration_seconds,
        ingestion_scope,
        is_test_data,
        message,
        sp_ingest_created_utc
    )
    SELECT
        audit_id,
        config_id,
        workflow_id,
        process_id,
        file_name,
        destination_database,
        destination_table,
        status,
        records_loaded,
        rows_scanned,
        validation_error_count,
        memory_peak_mb,
        duration_seconds,
        ingestion_scope,
        is_test_data,
        message,
        sp_ingest_created_utc
    FROM log.sharepoint_ingestion_audit;

    SET IDENTITY_INSERT log.sharepoint_ingestion_audit_reorder OFF;

    -- Step 3: drop the old table
    DROP TABLE log.sharepoint_ingestion_audit;

    -- Step 4: rename the shadow table to the canonical name
    EXEC sp_rename 'log.sharepoint_ingestion_audit_reorder', 'sharepoint_ingestion_audit';

    COMMIT TRANSACTION;
    PRINT 'Migration complete: destination_database and destination_table now precede status.';

END TRY
BEGIN CATCH
    ROLLBACK TRANSACTION;
    DECLARE @msg NVARCHAR(4000) = ERROR_MESSAGE();
    RAISERROR('Migration failed and was rolled back. Error: %s', 16, 1, @msg);
END CATCH
GO

-- ── Verification ──────────────────────────────────────────────────────────
SELECT
    c.column_id   AS ordinal,
    c.name        AS column_name,
    t.name        AS data_type,
    c.is_nullable
FROM   sys.columns c
JOIN   sys.types   t ON t.user_type_id = c.user_type_id
WHERE  c.object_id = OBJECT_ID('log.sharepoint_ingestion_audit')
ORDER  BY c.column_id;
GO
