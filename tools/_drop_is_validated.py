"""One-shot: drop is_validated (and its default constraint) from config and audit tables."""
import sys
sys.path.insert(0, ".")
from sharepoint_ingest.config import load_settings
from sharepoint_ingest.sql_client import SqlClient

s = load_settings(env_override="dev")
sql = SqlClient(s.sql)

drop_sql = """
DECLARE @sql NVARCHAR(MAX) = N'';

-- config.sharepoint_ingestion
IF COL_LENGTH('config.sharepoint_ingestion', 'is_validated') IS NOT NULL
BEGIN
    SELECT @sql = 'ALTER TABLE config.sharepoint_ingestion DROP CONSTRAINT [' + dc.name + ']'
    FROM sys.default_constraints dc
    JOIN sys.columns c ON c.object_id = dc.parent_object_id AND c.column_id = dc.parent_column_id
    JOIN sys.tables t ON t.object_id = c.object_id
    JOIN sys.schemas sc ON sc.schema_id = t.schema_id
    WHERE sc.name = 'config' AND t.name = 'sharepoint_ingestion' AND c.name = 'is_validated';

    IF @sql <> '' EXEC sp_executesql @sql;

    ALTER TABLE config.sharepoint_ingestion DROP COLUMN is_validated;
    PRINT 'config.sharepoint_ingestion.is_validated dropped';
END
ELSE
    PRINT 'config.sharepoint_ingestion.is_validated already absent';

SET @sql = N'';

-- log.sharepoint_ingestion_audit
IF COL_LENGTH('log.sharepoint_ingestion_audit', 'is_validated') IS NOT NULL
BEGIN
    SELECT @sql = 'ALTER TABLE log.sharepoint_ingestion_audit DROP CONSTRAINT [' + dc.name + ']'
    FROM sys.default_constraints dc
    JOIN sys.columns c ON c.object_id = dc.parent_object_id AND c.column_id = dc.parent_column_id
    JOIN sys.tables t ON t.object_id = c.object_id
    JOIN sys.schemas sc ON sc.schema_id = t.schema_id
    WHERE sc.name = 'log' AND t.name = 'sharepoint_ingestion_audit' AND c.name = 'is_validated';

    IF @sql <> '' EXEC sp_executesql @sql;

    ALTER TABLE log.sharepoint_ingestion_audit DROP COLUMN is_validated;
    PRINT 'log.sharepoint_ingestion_audit.is_validated dropped';
END
ELSE
    PRINT 'log.sharepoint_ingestion_audit.is_validated already absent';
"""

sql.execute(drop_sql)
print("Done — is_validated removed from both tables.")
