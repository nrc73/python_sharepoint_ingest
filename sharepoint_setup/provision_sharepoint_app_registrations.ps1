param(
    [ValidateSet("dev", "prod", "all")]
    [string]$Env = "all",
    [string]$SubscriptionId = "",
    [string]$TenantId = "",
    [string]$ResourceGroup = "resource_ingest",
    [string]$DevVaultName = "kv-sp-ingest-dev",
    [string]$ProdVaultName = "kv-sp-ingest-prod",
    [string]$DevSiteUrl = "https://mycompany715.sharepoint.com/sites/data_ingest_dev",
    [string]$ProdSiteUrl = "https://mycompany715.sharepoint.com/sites/data_ingestion_prod",
    [string]$DevSqlServer = "localhost:1433",
    [string]$DevSqlDatabase = "ingest_dev",
    [string]$ProdSqlServer = "localhost:1433",
    [string]$ProdSqlDatabase = "ingest_prod",
    [switch]$RotateClientSecrets,
    [switch]$SkipAdminConsent,
    [switch]$SkipSiteGrant
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

function Invoke-AzRaw {
    param([string[]]$Arguments)

    $previousErrorPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & az @Arguments 2>&1
    }
    finally {
        $ErrorActionPreference = $previousErrorPreference
    }

    [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = (($output | ForEach-Object { $_.ToString() }) -join "`n") }
}

function Invoke-AzJson {
    param([string[]]$Arguments, [switch]$AllowFailure)
    $r = Invoke-AzRaw -Arguments ($Arguments + @("--output", "json"))
    if ($r.ExitCode -ne 0) {
        if ($AllowFailure) { return [pscustomobject]@{ Success = $false; Data = $null; Error = $r.Output } }
        throw "az $($Arguments -join ' ') failed: $($r.Output)"
    }
    if ([string]::IsNullOrWhiteSpace($r.Output)) { return [pscustomobject]@{ Success = $true; Data = $null; Error = $null } }
    [pscustomobject]@{ Success = $true; Data = ($r.Output | ConvertFrom-Json); Error = $null }
}

function Normalize-Url([string]$u) { return (($u -replace '\p{Cf}', '').Trim()) }

function Get-ValueFromSources([string]$Name) {
    $v = [Environment]::GetEnvironmentVariable($Name)
    if (-not [string]::IsNullOrWhiteSpace($v)) { return $v }
    return $null
}

function Resolve-ParamOrEnv([string]$ProvidedValue, [string[]]$EnvVarNames, [string]$DefaultValue = "") {
    if (-not [string]::IsNullOrWhiteSpace($ProvidedValue)) { return $ProvidedValue }
    foreach ($n in $EnvVarNames) {
        $resolved = Get-ValueFromSources -Name $n
        if (-not [string]::IsNullOrWhiteSpace($resolved)) { return $resolved }
    }
    return $DefaultValue
}

function Set-KvSecret([string]$VaultName, [string]$Name, [string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { throw "Secret value for '$Name' is empty." }
    $s = Invoke-AzRaw -Arguments @("keyvault", "secret", "set", "--vault-name", $VaultName, "--name", $Name, "--value", $Value)
    if ($s.ExitCode -ne 0) { throw "Failed setting secret '$Name': $($s.Output)" }
    Write-Host "[PASS] KeyVault secret set: $Name" -ForegroundColor Green
}

function Get-SecretIfExists([string]$VaultName, [string]$Name) {
    $r = Invoke-AzRaw -Arguments @("keyvault", "secret", "show", "--vault-name", $VaultName, "--name", $Name, "--query", "value", "--output", "tsv")
    if ($r.ExitCode -ne 0) { return $null }
    return $r.Output.Trim()
}

function Extract-CredentialValue([string]$RawOutput) {
    if ([string]::IsNullOrWhiteSpace($RawOutput)) { return "" }

    $lines = $RawOutput -split "`r?`n"
    $filtered = @()

    foreach ($line in $lines) {
        $trimmed = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($trimmed)) { continue }
        if ($trimmed.StartsWith("WARNING:", [System.StringComparison]::OrdinalIgnoreCase)) { continue }
        $filtered += $trimmed
    }

    if ($filtered.Count -eq 0) { return "" }
    return $filtered[-1]
}

$SubscriptionId = Resolve-ParamOrEnv -ProvidedValue $SubscriptionId -EnvVarNames @("AZURE_SUBSCRIPTION_ID", "AZURE_SUBSCRIPTION")
if ([string]::IsNullOrWhiteSpace($SubscriptionId)) {
    throw "Subscription ID is required. Set AZURE_SUBSCRIPTION_ID in environment or pass -SubscriptionId."
}

$account = Invoke-AzJson -Arguments @("account", "show")
if ($account.Data.id -ne $SubscriptionId) {
    $set = Invoke-AzRaw -Arguments @("account", "set", "--subscription", $SubscriptionId)
    if ($set.ExitCode -ne 0) { throw "Unable to switch subscription: $($set.Output)" }
}

if ([string]::IsNullOrWhiteSpace($TenantId)) {
    $TenantId = Resolve-ParamOrEnv -ProvidedValue $TenantId -EnvVarNames @("AZURE_TENANT_ID") -DefaultValue ([string]$account.Data.tenantId)
}

# ── SharePoint Online resource (SPO) ────────────────────────────────────────
$sharePointSpAppId = "00000003-0000-0ff1-ce00-000000000000"

$sharePointSp = Invoke-AzJson -Arguments @("ad", "sp", "show", "--id", $sharePointSpAppId)
$sharePointSitesSelectedRole = @($sharePointSp.Data.appRoles | Where-Object { $_.value -eq "Sites.Selected" -and ($_.allowedMemberTypes -contains "Application") })[0]
if ($null -eq $sharePointSitesSelectedRole) { throw "Could not resolve SharePoint app role ID for Sites.Selected" }
$sharePointSitesSelectedRoleId = [string]$sharePointSitesSelectedRole.id

# ── Microsoft Graph resource ─────────────────────────────────────────────────
# Sites.ReadWrite.All on Graph (00000003-0000-0000-c000-000000000000) is the
# REQUIRED permission for the Graph API auth path used by SharePointClient.
# The legacy SharePoint REST (/_api/) path is blocked on this tenant by the
# x-ms-suspended-features app-only gate — the Graph path bypasses it entirely.
$graphSpAppId  = "00000003-0000-0000-c000-000000000000"
$graphSitesReadWriteAllRoleId = "9492366f-7969-46a4-8d15-ed1a20078fff"
$graphSp = Invoke-AzJson -Arguments @("ad", "sp", "show", "--id", $graphSpAppId)
$graphSpObjectId = [string]$graphSp.Data.id

$envs = if ($Env -eq "all") { @("dev", "prod") } else { @($Env) }

foreach ($e in $envs) {
    Write-Host "`n==== Provisioning $e ====" -ForegroundColor Cyan

    $targetVaultName = if ($e -eq "dev") {
        Resolve-ParamOrEnv -ProvidedValue $DevVaultName -EnvVarNames @("KEY_VAULT_NAME_DEV") -DefaultValue "kv-sp-ingest-dev"
    } else {
        Resolve-ParamOrEnv -ProvidedValue $ProdVaultName -EnvVarNames @("KEY_VAULT_NAME_PROD") -DefaultValue "kv-sp-ingest-prod"
    }

    $displayName = "spn-sharepoint-ingest-$e"
    $clientIdSecretName = "dm-sharepoint-$e-client-id"
    $clientSecretSecretName = "dm-sharepoint-$e-client-secret"
    $tenantSecretName = "dm-sharepoint-$e-tenant-id"
    $siteSecretName = "dm-sharepoint-$e-site-url"
    $sqlServerSecretName = "dm-sql-$e-server"
    $sqlDbSecretName = "dm-sql-$e-database"

    $siteUrl = if ($e -eq "dev") { Normalize-Url $DevSiteUrl } else { Normalize-Url $ProdSiteUrl }
    $sqlServer = if ($e -eq "dev") { $DevSqlServer } else { $ProdSqlServer }
    $sqlDb = if ($e -eq "dev") { $DevSqlDatabase } else { $ProdSqlDatabase }

    $appLookup = Invoke-AzJson -Arguments @("ad", "app", "list", "--display-name", $displayName, "--query", "[?displayName=='$displayName'] | [0]")
    $app = $appLookup.Data
    if ($null -eq $app) {
        $appCreate = Invoke-AzJson -Arguments @("ad", "app", "create", "--display-name", $displayName, "--sign-in-audience", "AzureADMyOrg")
        $app = $appCreate.Data
        Write-Host "[PASS] Created app registration: $displayName" -ForegroundColor Green
    } else {
        Write-Host "[PASS] Reusing app registration: $displayName" -ForegroundColor Green
    }

    $appId = [string]$app.appId
    Set-KvSecret -VaultName $targetVaultName -Name $clientIdSecretName -Value $appId

    $spShow = Invoke-AzJson -Arguments @("ad", "sp", "show", "--id", $appId) -AllowFailure
    if (-not $spShow.Success) {
        $spCreate = Invoke-AzRaw -Arguments @("ad", "sp", "create", "--id", $appId)
        if ($spCreate.ExitCode -ne 0) { throw "Failed creating service principal for ${displayName}: $($spCreate.Output)" }
        Write-Host "[PASS] Created service principal for $displayName" -ForegroundColor Green
    } else {
        Write-Host "[PASS] Service principal exists for $displayName" -ForegroundColor Green
    }

    $permList = Invoke-AzJson -Arguments @("ad", "app", "permission", "list", "--id", $appId)
    $hasSharePointSitesSelected = $false
    foreach ($rp in @($permList.Data)) {
        if ($rp.resourceAppId -eq $sharePointSpAppId) {
            foreach ($ra in @($rp.resourceAccess)) {
                if ([string]$ra.id -eq $sharePointSitesSelectedRoleId) { $hasSharePointSitesSelected = $true }
            }
        }
    }
    if (-not $hasSharePointSitesSelected) {
        $addSharePointPerm = Invoke-AzRaw -Arguments @("ad", "app", "permission", "add", "--id", $appId, "--api", $sharePointSpAppId, "--api-permissions", "$sharePointSitesSelectedRoleId=Role")
        if ($addSharePointPerm.ExitCode -ne 0) { throw "Failed adding SharePoint Sites.Selected to ${displayName}: $($addSharePointPerm.Output)" }
        Write-Host "[PASS] Added SharePoint Sites.Selected to $displayName" -ForegroundColor Green
    } else {
        Write-Host "[PASS] SharePoint Sites.Selected already assigned for $displayName" -ForegroundColor Green
    }

    if (-not $SkipAdminConsent.IsPresent) {
        $consent = Invoke-AzRaw -Arguments @("ad", "app", "permission", "admin-consent", "--id", $appId)
        if ($consent.ExitCode -ne 0) {
            Write-Host "[WARN] Admin consent failed for $displayName. You may need admin rights. Details: $($consent.Output)" -ForegroundColor Yellow
        } else {
            Write-Host "[PASS] Admin consent granted for $displayName" -ForegroundColor Green
        }
    }

    # ── Graph Sites.ReadWrite.All AppRoleAssignment (REQUIRED for Graph API path) ──
    # The SharePointClient uses Graph API (not SPO REST) because the SPO REST
    # /_api/ endpoint is blocked on this tenant by the x-ms-suspended-features gate.
    $spObjectId = [string](Invoke-AzJson -Arguments @("ad", "sp", "show", "--id", $appId)).Data.id
    $existingGraphRoles = Invoke-AzJson -Arguments @(
        "rest", "--method", "GET",
        "--url", "https://graph.microsoft.com/v1.0/servicePrincipals/$spObjectId/appRoleAssignments"
    )
    $hasGraphSitesReadWrite = ($existingGraphRoles.Data.value | Where-Object { $_.appRoleId -eq $graphSitesReadWriteAllRoleId -and $_.resourceId -eq $graphSpObjectId }) -ne $null
    if (-not $hasGraphSitesReadWrite) {
        $assignBody = "{`"principalId`":`"$spObjectId`",`"resourceId`":`"$graphSpObjectId`",`"appRoleId`":`"$graphSitesReadWriteAllRoleId`"}"
        $assignResult = Invoke-AzRaw -Arguments @(
            "rest", "--method", "POST",
            "--url", "https://graph.microsoft.com/v1.0/servicePrincipals/$spObjectId/appRoleAssignments",
            "--body", $assignBody
        )
        if ($assignResult.ExitCode -ne 0) {
            Write-Host "[WARN] Failed to assign Graph Sites.ReadWrite.All to $displayName. Details: $($assignResult.Output)" -ForegroundColor Yellow
        } else {
            Write-Host "[PASS] Graph Sites.ReadWrite.All AppRoleAssignment created for $displayName" -ForegroundColor Green
        }
    } else {
        Write-Host "[PASS] Graph Sites.ReadWrite.All already assigned for $displayName" -ForegroundColor Green
    }

    $existingClientSecret = Get-SecretIfExists -VaultName $targetVaultName -Name $clientSecretSecretName
    if ($RotateClientSecrets.IsPresent -or [string]::IsNullOrWhiteSpace($existingClientSecret)) {
        $newSecret = Invoke-AzRaw -Arguments @("ad", "app", "credential", "reset", "--id", $appId, "--append", "--display-name", "kv-$e-$(Get-Date -Format yyyyMMddHHmmss)", "--years", "2", "--query", "password", "--output", "tsv", "--only-show-errors")
        $newSecretValue = Extract-CredentialValue -RawOutput $newSecret.Output
        if ($newSecret.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($newSecretValue)) {
            throw "Failed generating client secret for ${displayName}: $($newSecret.Output)"
        }
        Set-KvSecret -VaultName $targetVaultName -Name $clientSecretSecretName -Value $newSecretValue
        Write-Host "[PASS] Generated and stored new client secret for $displayName" -ForegroundColor Green
    } else {
        Write-Host "[PASS] Existing Key Vault client secret retained for $displayName" -ForegroundColor Green
    }

    Set-KvSecret -VaultName $targetVaultName -Name $tenantSecretName -Value $TenantId
    Set-KvSecret -VaultName $targetVaultName -Name $siteSecretName -Value $siteUrl
    Set-KvSecret -VaultName $targetVaultName -Name $sqlServerSecretName -Value $sqlServer
    Set-KvSecret -VaultName $targetVaultName -Name $sqlDbSecretName -Value $sqlDb

    if (-not $SkipSiteGrant.IsPresent) {
        Write-Host "[INFO] Site grant step is now manual and SharePoint-only." -ForegroundColor Cyan
        Write-Host "[INFO] Run the following in PowerShell 7 as a SharePoint admin:" -ForegroundColor Cyan
        Write-Host "       Connect-PnPOnline -Url \"https://mycompany715-admin.sharepoint.com\" -Interactive" -ForegroundColor Cyan
        Write-Host "       Grant-PnPAzureADAppSitePermission -AppId \"$appId\" -DisplayName \"$displayName\" -Site \"$siteUrl\" -Permissions Write" -ForegroundColor Cyan
        Write-Host "       Get-PnPAzureADAppSitePermission -Site \"$siteUrl\"" -ForegroundColor Cyan
    } else {
        Write-Host "[PASS] Site grant step skipped by request (-SkipSiteGrant)." -ForegroundColor Green
    }
}

Write-Host "`nProvisioning workflow complete." -ForegroundColor Cyan