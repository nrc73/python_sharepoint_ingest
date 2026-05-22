/*
Local development Database Mail profile for smtp4dev.

Runs in regular SSMS query mode (no sqlcmd :setvar required).

Before running:
1) Start smtp4dev (PowerShell):
     powershell -ExecutionPolicy Bypass -File .\sharepoint_setup\create_local_smtp4dev.ps1
2) Use localhost SMTP endpoint for desktop SQL Server.
*/

USE [msdb];
GO

DECLARE @ProfileName SYSNAME = N'Dev Local SMTP';
DECLARE @AccountName SYSNAME = N'Dev Local SMTP Account';
DECLARE @Description NVARCHAR(255) = N'Local smtp4dev profile for SharePoint ingestion Layer 5 testing';
DECLARE @EmailAddress NVARCHAR(320) = N'sharepoint-ingest-dev@localhost';
DECLARE @ReplyToAddress NVARCHAR(320) = N'sharepoint-ingest-dev@localhost';
DECLARE @DisplayName NVARCHAR(128) = N'SharePoint Ingest Dev';
DECLARE @SmtpServer NVARCHAR(255) = N'localhost';
DECLARE @SmtpPort INT = 2525;
DECLARE @TestRecipient NVARCHAR(320) = N'dev-test@local.invalid';

-- Enable DB Mail XPs when disabled
EXEC sp_configure 'show advanced options', 1;
RECONFIGURE;
EXEC sp_configure 'Database Mail XPs', 1;
RECONFIGURE;
GO

DECLARE @ProfileName SYSNAME = N'Dev Local SMTP';
DECLARE @AccountName SYSNAME = N'Dev Local SMTP Account';
DECLARE @Description NVARCHAR(255) = N'Local smtp4dev profile for SharePoint ingestion Layer 5 testing';
DECLARE @EmailAddress NVARCHAR(320) = N'sharepoint-ingest-dev@localhost';
DECLARE @ReplyToAddress NVARCHAR(320) = N'sharepoint-ingest-dev@localhost';
DECLARE @DisplayName NVARCHAR(128) = N'SharePoint Ingest Dev';
DECLARE @SmtpServer NVARCHAR(255) = N'localhost';
DECLARE @SmtpPort INT = 2525;

DECLARE @account_id INT = (
    SELECT account_id
    FROM msdb.dbo.sysmail_account
    WHERE [name] = @AccountName
);

IF @account_id IS NULL
BEGIN
    EXEC msdb.dbo.sysmail_add_account_sp
        @account_name = @AccountName,
        @description = @Description,
        @email_address = @EmailAddress,
        @replyto_address = @ReplyToAddress,
        @display_name = @DisplayName,
        @mailserver_name = @SmtpServer,
        @port = @SmtpPort,
        @enable_ssl = 0;
END
ELSE
BEGIN
    EXEC msdb.dbo.sysmail_update_account_sp
        @account_name = @AccountName,
        @description = @Description,
        @email_address = @EmailAddress,
        @replyto_address = @ReplyToAddress,
        @display_name = @DisplayName,
        @mailserver_name = @SmtpServer,
        @port = @SmtpPort,
        @enable_ssl = 0;
END

IF NOT EXISTS (
    SELECT 1
    FROM msdb.dbo.sysmail_profile
    WHERE [name] = @ProfileName
)
BEGIN
    EXEC msdb.dbo.sysmail_add_profile_sp
        @profile_name = @ProfileName,
        @description = @Description;
END

IF NOT EXISTS (
    SELECT 1
    FROM msdb.dbo.sysmail_profileaccount pa
    INNER JOIN msdb.dbo.sysmail_profile p ON p.profile_id = pa.profile_id
    INNER JOIN msdb.dbo.sysmail_account a ON a.account_id = pa.account_id
    WHERE p.[name] = @ProfileName
      AND a.[name] = @AccountName
)
BEGIN
    EXEC msdb.dbo.sysmail_add_profileaccount_sp
        @profile_name = @ProfileName,
        @account_name = @AccountName,
        @sequence_number = 1;
END

IF NOT EXISTS (
    SELECT 1
    FROM msdb.dbo.sysmail_principalprofile pp
    INNER JOIN msdb.sys.database_principals dp ON pp.principal_sid = dp.sid
    INNER JOIN msdb.dbo.sysmail_profile p ON p.profile_id = pp.profile_id
    WHERE dp.name = N'public'
      AND p.[name] = @ProfileName
)
BEGIN
    EXEC msdb.dbo.sysmail_add_principalprofile_sp
        @profile_name = @ProfileName,
        @principal_name = N'public',
        @is_default = 0;
END

DECLARE @mailitem_id INT;
DECLARE @TestRecipient NVARCHAR(320) = N'dev-test@local.invalid';

EXEC msdb.dbo.sp_send_dbmail
    @profile_name = @ProfileName,
    @recipients = @TestRecipient,
    @subject = N'SharePoint ingest Layer 5 local smtp4dev test',
    @body = N'This is a local smtp4dev test email from SQL Database Mail.',
    @mailitem_id = @mailitem_id OUTPUT;

SELECT @mailitem_id AS mailitem_id;

SELECT TOP (1)
    mailitem_id,
    recipients,
    [subject],
    sent_status,
    send_request_date,
    last_mod_date
FROM msdb.dbo.sysmail_allitems
WHERE mailitem_id = @mailitem_id
ORDER BY mailitem_id DESC;
