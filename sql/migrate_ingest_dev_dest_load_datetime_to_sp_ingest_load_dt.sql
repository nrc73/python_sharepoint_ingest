IF DB_ID('ingest_dev') IS NULL
BEGIN
    RAISERROR('Database ingest_dev does not exist.', 16, 1);
    RETURN;
END
GO

USE ingest_dev;
GO

IF DB_NAME() <> 'ingest_dev'
BEGIN
    RAISERROR('Guard rail violation: this migration must be executed against ingest_dev only.', 16, 1);
    RETURN;
END
GO

DECLARE @renames TABLE (
    table_schema SYSNAME NOT NULL,
    table_name SYSNAME NOT NULL,
    from_column SYSNAME NOT NULL,
    to_column SYSNAME NOT NULL
);

INSERT INTO @renames (table_schema, table_name, from_column, to_column)
SELECT c.TABLE_SCHEMA, c.TABLE_NAME, c.COLUMN_NAME, 'sp_ingest_load_dt'
FROM INFORMATION_SCHEMA.COLUMNS c
WHERE c.TABLE_SCHEMA = 'sharepoint'
  AND c.TABLE_NAME LIKE 'dest[_]%'
  AND c.COLUMN_NAME = 'load_datetime'
  AND NOT EXISTS (
      SELECT 1
      FROM INFORMATION_SCHEMA.COLUMNS c2
      WHERE c2.TABLE_SCHEMA = c.TABLE_SCHEMA
        AND c2.TABLE_NAME = c.TABLE_NAME
        AND c2.COLUMN_NAME = 'sp_ingest_load_dt'
  );

INSERT INTO @renames (table_schema, table_name, from_column, to_column)
SELECT c.TABLE_SCHEMA, c.TABLE_NAME, c.COLUMN_NAME, 'sp_ingest_load_dt'
FROM INFORMATION_SCHEMA.COLUMNS c
WHERE c.TABLE_SCHEMA = 'sharepoint'
  AND c.TABLE_NAME LIKE 'dest[_]%'
  AND c.COLUMN_NAME = 'sp_ingest_created_utc'
  AND NOT EXISTS (
      SELECT 1
      FROM INFORMATION_SCHEMA.COLUMNS c2
      WHERE c2.TABLE_SCHEMA = c.TABLE_SCHEMA
        AND c2.TABLE_NAME = c.TABLE_NAME
        AND c2.COLUMN_NAME = 'sp_ingest_load_dt'
  );

DECLARE
    @schema SYSNAME,
    @table SYSNAME,
    @from_col SYSNAME,
    @to_col SYSNAME,
    @sql NVARCHAR(MAX);

DECLARE rename_cursor CURSOR LOCAL FAST_FORWARD FOR
SELECT table_schema, table_name, from_column, to_column
FROM @renames
ORDER BY table_schema, table_name,
         CASE WHEN from_column = 'load_datetime' THEN 1 ELSE 2 END;

OPEN rename_cursor;
FETCH NEXT FROM rename_cursor INTO @schema, @table, @from_col, @to_col;

WHILE @@FETCH_STATUS = 0
BEGIN
    SET @sql = N'EXEC sp_rename '''
        + QUOTENAME(@schema) + N'.' + QUOTENAME(@table) + N'.' + QUOTENAME(@from_col)
        + N''', ''' + @to_col + N''', ''COLUMN'';';

    PRINT N'Renaming ' + QUOTENAME(@schema) + N'.' + QUOTENAME(@table)
        + N'.' + QUOTENAME(@from_col) + N' -> ' + QUOTENAME(@to_col);

    EXEC sys.sp_executesql @sql;

    FETCH NEXT FROM rename_cursor INTO @schema, @table, @from_col, @to_col;
END

CLOSE rename_cursor;
DEALLOCATE rename_cursor;

SELECT
    c.TABLE_SCHEMA,
    c.TABLE_NAME,
    c.COLUMN_NAME
FROM INFORMATION_SCHEMA.COLUMNS c
WHERE c.TABLE_SCHEMA = 'sharepoint'
  AND c.TABLE_NAME LIKE 'dest[_]%'
  AND c.COLUMN_NAME IN ('sp_ingest_load_dt', 'load_datetime', 'sp_ingest_created_utc')
ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME, c.COLUMN_NAME;
GO