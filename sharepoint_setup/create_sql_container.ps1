param(
    [string]$ContainerName = "sql2022-ingest",
    [string]$SaPassword = $env:SQL_SERVER_PASSWORD,
    [string]$HostPort = "1433",
    [string]$SqlImage = "mcr.microsoft.com/mssql/server:2022-latest",
    [string]$DataVolumeName = "sql2022-ingest-data",
    [string]$DevDatabaseName = "ingest_dev",
    [string]$ProdDatabaseName = "ingest_prod",
    [switch]$ResetUserDatabases
)

if ([string]::IsNullOrWhiteSpace($SaPassword)) {
    $SaPassword = "YourStrong!Passw0rd1"
}

function Invoke-SqlInContainer {
    param(
        [string]$Query,
        [string]$Database = "master"
    )

    docker exec $ContainerName /opt/mssql-tools18/bin/sqlcmd -S localhost -d $Database -U sa -P "$SaPassword" -C -b -Q $Query | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "SQL execution failed for database [$Database]."
    }
}

function Ensure-DatabaseSchema {
    param(
        [string]$DatabaseName
    )

    $schemaSql = @"
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'config')
BEGIN
    EXEC('CREATE SCHEMA [config] AUTHORIZATION [dbo]');
END;

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'log')
BEGIN
    EXEC('CREATE SCHEMA [log] AUTHORIZATION [dbo]');
END;

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
        created_date DATETIME DEFAULT GETDATE(),
        modified_date DATETIME DEFAULT GETDATE()
    );
END;

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
        message VARCHAR(MAX) NULL,
        created_date DATETIME NOT NULL DEFAULT GETDATE()
    );
END;

IF OBJECT_ID('dbo.sample_ingestion_target', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.sample_ingestion_target (
        business_key VARCHAR(50) NOT NULL,
        name VARCHAR(200) NULL,
        amount DECIMAL(18,2) NULL,
        effective_date DATE NULL,
        source_file_name VARCHAR(255) NULL,
        created_date DATETIME NOT NULL DEFAULT GETDATE(),
        modified_date DATETIME NOT NULL DEFAULT GETDATE(),
        CONSTRAINT PK_sample_ingestion_target PRIMARY KEY (business_key)
    );
END;
"@

    Invoke-SqlInContainer -Query $schemaSql -Database $DatabaseName
}

Write-Host "Checking Docker availability..."
docker version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Docker is not available. Ensure Docker Desktop is running."
}

Write-Host "Ensuring Docker volume exists for SQL persistence: $DataVolumeName"
docker volume create $DataVolumeName | Out-Null

Write-Host "Removing existing container (if any): $ContainerName"
docker rm -f $ContainerName 2>$null | Out-Null

Write-Host "Starting SQL Server container $ContainerName on localhost:$HostPort"
docker run -d `
  --name $ContainerName `
  -e "ACCEPT_EULA=Y" `
  -e "MSSQL_SA_PASSWORD=$SaPassword" `
  -e "MSSQL_PID=Developer" `
  -v "${DataVolumeName}:/var/opt/mssql" `
  -p "$HostPort`:1433" `
  $SqlImage | Out-Null

if ($LASTEXITCODE -ne 0) {
    throw "Failed to start SQL container."
}

Write-Host "Waiting for SQL Server to accept connections..."
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    docker exec $ContainerName /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P "$SaPassword" -C -Q "SELECT 1" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $ready = $true
        break
    }
}

if (-not $ready) {
    throw "SQL Server did not become ready in time."
}

if ($ResetUserDatabases.IsPresent) {
    Write-Host "Reset flag provided. Dropping existing user databases..."
    $dropUserDatabasesSql = @"
DECLARE @sql NVARCHAR(MAX) = N'';
SELECT @sql += N'ALTER DATABASE [' + [name] + N'] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [' + [name] + N'];'
FROM sys.databases
WHERE database_id > 4;

IF LEN(@sql) > 0
BEGIN
    EXEC sp_executesql @sql;
END;
"@
    Invoke-SqlInContainer -Query $dropUserDatabasesSql -Database "master"
}

$targetDatabases = @($DevDatabaseName, $ProdDatabaseName) | Select-Object -Unique

foreach ($dbName in $targetDatabases) {
    Write-Host "Ensuring database [$dbName] exists..."
    $createDbSql = "IF DB_ID('$dbName') IS NULL CREATE DATABASE [$dbName];"
    Invoke-SqlInContainer -Query $createDbSql -Database "master"

    Write-Host "Ensuring required tables exist in [$dbName]..."
    Ensure-DatabaseSchema -DatabaseName $dbName
}



Write-Host ""
Write-Host "SQL container is ready, persistent, and SSMS-accessible."
Write-Host "SSMS Server Name   : localhost,$HostPort"
Write-Host "Authentication     : SQL Server Authentication"
Write-Host "Login              : sa"
Write-Host "Databases          : $($targetDatabases -join ', ')"
Write-Host "Docker Volume      : $DataVolumeName"
