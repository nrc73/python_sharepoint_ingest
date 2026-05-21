param(
    [ValidateSet("dev", "prod", "all")]
    [string]$Env = "prod",
    [switch]$TestSecrets,
    [switch]$OutputSecretValues,
    [switch]$FixContext,
    [switch]$SkipDotEnv,
    [string]$VaultName,
    [string]$VaultUrl,
    [string]$ResourceGroup,
    [string]$SubscriptionId,
    [string]$TenantId
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

function Write-Info {
    param([string]$Message)
    Write-Host "[INFO] $Message" -ForegroundColor Cyan
}

function Write-Section {
    param([string]$Message)
    Write-Host ""
    Write-Host "==== $Message ====" -ForegroundColor Magenta
}

function New-EnvSummary {
    param([string]$EnvironmentName)
    return [pscustomobject]@{ Env = $EnvironmentName; Pass = 0; Warn = 0; Fail = 0 }
}

function Add-Result {
    param(
        [pscustomobject]$Summary,
        [ValidateSet("PASS", "WARN", "FAIL")]
        [string]$Level,
        [string]$Message
    )

    switch ($Level) {
        "PASS" { $Summary.Pass++; Write-Host "[PASS][$($Summary.Env)] $Message" -ForegroundColor Green }
        "WARN" { $Summary.Warn++; Write-Host "[WARN][$($Summary.Env)] $Message" -ForegroundColor Yellow }
        "FAIL" { $Summary.Fail++; Write-Host "[FAIL][$($Summary.Env)] $Message" -ForegroundColor Red }
    }
}

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

    $exitCode = $LASTEXITCODE
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output   = (($output | ForEach-Object { $_.ToString() }) -join "`n")
    }
}

function Invoke-AzJson {
    param(
        [string[]]$Arguments,
        [switch]$AllowFailure
    )

    $result = Invoke-AzRaw -Arguments ($Arguments + @("--output", "json"))
    if ($result.ExitCode -ne 0) {
        if ($AllowFailure) {
            return [pscustomobject]@{ Success = $false; Data = $null; Error = $result.Output }
        }
        throw "az $($Arguments -join ' ') failed: $($result.Output)"
    }

    if ([string]::IsNullOrWhiteSpace($result.Output)) {
        return [pscustomobject]@{ Success = $true; Data = $null; Error = $null }
    }

    try {
        $json = $result.Output | ConvertFrom-Json -ErrorAction Stop
        return [pscustomobject]@{ Success = $true; Data = $json; Error = $null }
    }
    catch {
        if ($AllowFailure) {
            return [pscustomobject]@{
                Success = $false
                Data    = $null
                Error   = "Failed to parse JSON output from az command: $($result.Output)"
            }
        }
        throw
    }
}

function Load-DotEnvValues {
    param([string]$Path)
    $values = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $values }

    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($trimmed) -or $trimmed.StartsWith("#")) { continue }
        $separatorIndex = $trimmed.IndexOf("=")
        if ($separatorIndex -lt 1) { continue }

        $key = $trimmed.Substring(0, $separatorIndex).Trim()
        $value = $trimmed.Substring($separatorIndex + 1).Trim()
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            if ($value.Length -ge 2) { $value = $value.Substring(1, $value.Length - 2) }
        }
        $values[$key] = $value
    }

    return $values
}

function Get-ValueFromSources {
    param([string]$Name, [hashtable]$DotEnvValues)
    $processValue = [Environment]::GetEnvironmentVariable($Name)
    if (-not [string]::IsNullOrWhiteSpace($processValue)) { return $processValue }
    if ($DotEnvValues.ContainsKey($Name)) { return $DotEnvValues[$Name] }
    return $null
}

function Resolve-PerEnvironmentValue {
    param(
        [string]$BaseName,
        [string]$EnvironmentName,
        [hashtable]$DotEnvValues,
        [string]$DefaultValue = ""
    )

    $envKey = $EnvironmentName.ToUpperInvariant()
    $specificName = "{0}_{1}" -f $BaseName, $envKey
    $specificValue = Get-ValueFromSources -Name $specificName -DotEnvValues $DotEnvValues
    if (-not [string]::IsNullOrWhiteSpace($specificValue)) { return $specificValue }

    $globalValue = Get-ValueFromSources -Name $BaseName -DotEnvValues $DotEnvValues
    if (-not [string]::IsNullOrWhiteSpace($globalValue)) { return $globalValue }
    return $DefaultValue
}

function Get-VaultNameFromUrl {
    param([string]$Url)
    if ([string]::IsNullOrWhiteSpace($Url)) { return $null }
    $match = [regex]::Match($Url, '^https://([a-zA-Z0-9-]+)\.vault\.azure\.net/?$')
    if ($match.Success) { return $match.Groups[1].Value }
    return $null
}

function ConvertFrom-Base64Url {
    param([string]$Input)
    $padded = $Input.Replace("-", "+").Replace("_", "/")
    switch ($padded.Length % 4) {
        2 { $padded += "==" }
        3 { $padded += "=" }
    }
    $bytes = [Convert]::FromBase64String($padded)
    return [System.Text.Encoding]::UTF8.GetString($bytes)
}

function Get-AccessTokenClaims {
    $tokenResult = Invoke-AzRaw -Arguments @(
        "account", "get-access-token",
        "--resource", "https://vault.azure.net",
        "--query", "accessToken",
        "--output", "tsv"
    )

    if ($tokenResult.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($tokenResult.Output)) {
        return [pscustomobject]@{ Success = $false; Claims = $null; Error = $tokenResult.Output }
    }

    $token = $tokenResult.Output.Trim()
    $parts = $token.Split(".")
    if ($parts.Count -lt 2) {
        return [pscustomobject]@{ Success = $false; Claims = $null; Error = "Unexpected token format from az account get-access-token" }
    }

    try {
        $payloadJson = ConvertFrom-Base64Url -Input $parts[1]
        $claims = $payloadJson | ConvertFrom-Json -ErrorAction Stop
        return [pscustomobject]@{ Success = $true; Claims = $claims; Error = $null }
    }
    catch {
        return [pscustomobject]@{ Success = $false; Claims = $null; Error = "Unable to decode token claims: $($_.Exception.Message)" }
    }
}

function Get-SecretReadResult {
    param(
        [string]$VaultName,
        [string]$SecretName,
        [bool]$IsRequired,
        [bool]$OutputValues
    )

    if ([string]::IsNullOrWhiteSpace($SecretName)) {
        if ($IsRequired) {
            return [pscustomobject]@{ Level = "FAIL"; Message = "Required secret name is empty"; Detail = $null }
        }
        return [pscustomobject]@{ Level = "WARN"; Message = "Optional secret name is not configured; skipping read test"; Detail = $null }
    }

    $secretResult = Invoke-AzRaw -Arguments @(
        "keyvault", "secret", "show",
        "--vault-name", $VaultName,
        "--name", $SecretName,
        "--query", "value",
        "--output", "tsv"
    )

    if ($secretResult.ExitCode -ne 0) {
        $errorMessage = $secretResult.Output
        $reason = "unknown error"
        if ($errorMessage -match "ForbiddenByRbac|Forbidden") { $reason = "forbidden (RBAC/permissions)" }
        elseif ($errorMessage -match "SecretNotFound|was not found") { $reason = "secret not found" }

        return [pscustomobject]@{
            Level   = "FAIL"
            Message = "Secret '$SecretName' read failed: $reason"
            Detail  = $errorMessage
        }
    }

    $value = $secretResult.Output
    if ($null -eq $value) { $value = "" }
    $value = $value.TrimEnd("`r", "`n")

    if ([string]::IsNullOrWhiteSpace($value)) {
        return [pscustomobject]@{ Level = "FAIL"; Message = "Secret '$SecretName' was read but value is empty"; Detail = $null }
    }

    if ($OutputValues) {
        return [pscustomobject]@{ Level = "PASS"; Message = "Secret '$SecretName' value: $value"; Detail = $null }
    }

    $preview = if ($value.Length -le 6) { "*" * $value.Length } else { "{0}...{1}" -f $value.Substring(0, 3), $value.Substring($value.Length - 2, 2) }
    return [pscustomobject]@{
        Level   = "PASS"
        Message = "Secret '$SecretName' is non-empty (len=$($value.Length), preview=$preview)"
        Detail  = $null
    }
}

if ($OutputSecretValues.IsPresent -and -not $TestSecrets.IsPresent) {
    throw "-OutputSecretValues requires -TestSecrets."
}

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI (az) is not installed or not available on PATH."
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$dotEnvPath = Join-Path $repoRoot ".env"
$dotEnvValues = @{}

if (-not $SkipDotEnv.IsPresent) {
    if (Test-Path -LiteralPath $dotEnvPath) {
        $dotEnvValues = Load-DotEnvValues -Path $dotEnvPath
        Write-Info "Loaded configuration values from $dotEnvPath"
    }
    else {
        Write-Info "No .env file found at repo root. Using process environment values and script parameters."
    }
}

if ($OutputSecretValues.IsPresent) {
    Write-Section "DANGEROUS OUTPUT MODE"
    Write-Host "Secret plaintext values will be written to screen output." -ForegroundColor Red
    Write-Host "Ensure your terminal logs/screen recording/history are handled securely." -ForegroundColor Red
}

$accountResult = Invoke-AzJson -Arguments @("account", "show") -AllowFailure
if (-not $accountResult.Success -or $null -eq $accountResult.Data) {
    throw "Unable to read Azure CLI account context. Run 'az login' first. Details: $($accountResult.Error)"
}

$initialAccount = $accountResult.Data
Write-Info "Azure CLI account context detected: subscription='$($initialAccount.name)' ($($initialAccount.id)), tenant='$($initialAccount.tenantId)', user='$($initialAccount.user.name)'"

$targetEnvironments = if ($Env -eq "all") { @("dev", "prod") } else { @($Env) }
$secretReadRoleNames = @("Key Vault Secrets User", "Key Vault Secrets Officer", "Key Vault Administrator")

$overallSummaries = @()

foreach ($environmentName in $targetEnvironments) {
    Write-Section "Environment: $environmentName"
    $summary = New-EnvSummary -EnvironmentName $environmentName

    $resolvedSubscription = if (-not [string]::IsNullOrWhiteSpace($SubscriptionId)) {
        $SubscriptionId
    }
    else {
        $v = Resolve-PerEnvironmentValue -BaseName "AZURE_SUBSCRIPTION_ID" -EnvironmentName $environmentName -DotEnvValues $dotEnvValues
        if (-not [string]::IsNullOrWhiteSpace($v)) { $v } else { Resolve-PerEnvironmentValue -BaseName "AZURE_SUBSCRIPTION" -EnvironmentName $environmentName -DotEnvValues $dotEnvValues }
    }
    $resolvedTenant = if (-not [string]::IsNullOrWhiteSpace($TenantId)) { $TenantId } else { Resolve-PerEnvironmentValue -BaseName "AZURE_TENANT_ID" -EnvironmentName $environmentName -DotEnvValues $dotEnvValues }
    $resolvedResourceGroup = if (-not [string]::IsNullOrWhiteSpace($ResourceGroup)) { $ResourceGroup } else { Resolve-PerEnvironmentValue -BaseName "AZURE_RESOURCE_GROUP" -EnvironmentName $environmentName -DotEnvValues $dotEnvValues }
    $resolvedVaultName = if (-not [string]::IsNullOrWhiteSpace($VaultName)) { $VaultName } else { Resolve-PerEnvironmentValue -BaseName "KEY_VAULT_NAME" -EnvironmentName $environmentName -DotEnvValues $dotEnvValues }
    $resolvedVaultUrl = if (-not [string]::IsNullOrWhiteSpace($VaultUrl)) { $VaultUrl } else { Resolve-PerEnvironmentValue -BaseName "KEY_VAULT_URL" -EnvironmentName $environmentName -DotEnvValues $dotEnvValues }

    if ([string]::IsNullOrWhiteSpace($resolvedVaultName) -and -not [string]::IsNullOrWhiteSpace($resolvedVaultUrl)) {
        $resolvedVaultName = Get-VaultNameFromUrl -Url $resolvedVaultUrl
    }
    if ([string]::IsNullOrWhiteSpace($resolvedVaultUrl) -and -not [string]::IsNullOrWhiteSpace($resolvedVaultName)) {
        $resolvedVaultUrl = "https://$resolvedVaultName.vault.azure.net/"
    }

    if ([string]::IsNullOrWhiteSpace($resolvedVaultName)) {
        Add-Result -Summary $summary -Level "FAIL" -Message "Unable to resolve Key Vault name. Set KEY_VAULT_NAME[_ENV], pass -VaultName, or provide KEY_VAULT_URL[_ENV]/-VaultUrl."
        $overallSummaries += $summary
        continue
    }
    if ([string]::IsNullOrWhiteSpace($resolvedVaultUrl)) {
        Add-Result -Summary $summary -Level "FAIL" -Message "Unable to resolve Key Vault URL."
        $overallSummaries += $summary
        continue
    }

    Add-Result -Summary $summary -Level "PASS" -Message "Resolved vault name: $resolvedVaultName"
    Add-Result -Summary $summary -Level "PASS" -Message "Resolved vault URL : $resolvedVaultUrl"

    $accountNowResult = Invoke-AzJson -Arguments @("account", "show") -AllowFailure
    if (-not $accountNowResult.Success -or $null -eq $accountNowResult.Data) {
        Add-Result -Summary $summary -Level "FAIL" -Message "Unable to read current Azure account context: $($accountNowResult.Error)"
        $overallSummaries += $summary
        continue
    }

    $accountNow = $accountNowResult.Data
    $currentSubscriptionId = [string]$accountNow.id
    $currentSubscriptionName = [string]$accountNow.name
    $currentTenantId = [string]$accountNow.tenantId

    if (-not [string]::IsNullOrWhiteSpace($resolvedSubscription)) {
        $subscriptionMatches = ($currentSubscriptionId -ieq $resolvedSubscription)
        if (-not $subscriptionMatches -and $FixContext.IsPresent) {
            Add-Result -Summary $summary -Level "WARN" -Message "Azure subscription mismatch. Attempting 'az account set --subscription $resolvedSubscription'."
            $setResult = Invoke-AzRaw -Arguments @("account", "set", "--subscription", $resolvedSubscription)
            if ($setResult.ExitCode -ne 0) {
                Add-Result -Summary $summary -Level "FAIL" -Message "Failed to switch subscription: $($setResult.Output)"
            }
            else {
                $accountNowResult = Invoke-AzJson -Arguments @("account", "show") -AllowFailure
                if ($accountNowResult.Success -and $null -ne $accountNowResult.Data) {
                    $accountNow = $accountNowResult.Data
                    $currentSubscriptionId = [string]$accountNow.id
                    $currentSubscriptionName = [string]$accountNow.name
                    $currentTenantId = [string]$accountNow.tenantId
                    $subscriptionMatches = ($currentSubscriptionId -ieq $resolvedSubscription)
                }
            }
        }

        if ($subscriptionMatches) {
            Add-Result -Summary $summary -Level "PASS" -Message "Azure subscription context matches expected value '$resolvedSubscription'."
        }
        else {
            Add-Result -Summary $summary -Level "FAIL" -Message "Azure subscription mismatch. CurrentId='$currentSubscriptionId', ExpectedId='$resolvedSubscription'. (Current name: '$currentSubscriptionName')"
        }
    }
    else {
        Add-Result -Summary $summary -Level "WARN" -Message "Expected subscription is not configured (AZURE_SUBSCRIPTION_ID[_ENV] or -SubscriptionId)."
    }

    if (-not [string]::IsNullOrWhiteSpace($resolvedTenant)) {
        if ($currentTenantId -ieq $resolvedTenant) {
            Add-Result -Summary $summary -Level "PASS" -Message "Azure tenant context matches expected tenant '$resolvedTenant'."
        }
        else {
            Add-Result -Summary $summary -Level "FAIL" -Message "Azure tenant mismatch. Current='$currentTenantId', Expected='$resolvedTenant'."
        }
    }
    else {
        Add-Result -Summary $summary -Level "WARN" -Message "Expected tenant is not configured (AZURE_TENANT_ID[_ENV] or -TenantId)."
    }

    $vaultShowArgs = @("keyvault", "show", "--name", $resolvedVaultName)
    if (-not [string]::IsNullOrWhiteSpace($resolvedResourceGroup)) { $vaultShowArgs += @("--resource-group", $resolvedResourceGroup) }

    $vaultShowResult = Invoke-AzJson -Arguments $vaultShowArgs -AllowFailure
    $vaultResourceId = $null
    $vaultUriFromAzure = $null
    if (-not $vaultShowResult.Success -or $null -eq $vaultShowResult.Data) {
        Add-Result -Summary $summary -Level "FAIL" -Message "Failed to resolve Key Vault resource details: $($vaultShowResult.Error)"
    }
    else {
        $vaultResourceId = [string]$vaultShowResult.Data.id
        $vaultUriFromAzure = [string]$vaultShowResult.Data.properties.vaultUri
        Add-Result -Summary $summary -Level "PASS" -Message "Key Vault resource found: $vaultResourceId"

        if (-not [string]::IsNullOrWhiteSpace($vaultUriFromAzure) -and ($vaultUriFromAzure -ine $resolvedVaultUrl)) {
            Add-Result -Summary $summary -Level "WARN" -Message "Configured vault URL ('$resolvedVaultUrl') differs from Azure-reported URI ('$vaultUriFromAzure')."
        }
    }

    $claimsResult = Get-AccessTokenClaims
    $tokenObjectId = $null
    if (-not $claimsResult.Success -or $null -eq $claimsResult.Claims) {
        Add-Result -Summary $summary -Level "WARN" -Message "Could not decode access token claims: $($claimsResult.Error)"
    }
    else {
        $claims = $claimsResult.Claims
        $tokenObjectId = [string]$claims.oid
        $tokenAppId = [string]$claims.appid
        $tokenTenantId = [string]$claims.tid
        $tokenUpn = [string]$claims.upn

        $principalText = "Token principal: appid=$tokenAppId; oid=$tokenObjectId; tid=$tokenTenantId"
        if (-not [string]::IsNullOrWhiteSpace($tokenUpn)) { $principalText += "; upn=$tokenUpn" }
        Add-Result -Summary $summary -Level "PASS" -Message $principalText

        if (-not [string]::IsNullOrWhiteSpace($resolvedTenant) -and ($tokenTenantId -ine $resolvedTenant)) {
            Add-Result -Summary $summary -Level "FAIL" -Message "Token tenant '$tokenTenantId' does not match expected tenant '$resolvedTenant'."
        }
    }

    if (-not [string]::IsNullOrWhiteSpace($vaultResourceId) -and -not [string]::IsNullOrWhiteSpace($tokenObjectId)) {
        $roleResult = Invoke-AzJson -Arguments @(
            "role", "assignment", "list",
            "--assignee-object-id", $tokenObjectId,
            "--scope", $vaultResourceId,
            "--include-inherited"
        ) -AllowFailure

        if (-not $roleResult.Success) {
            Add-Result -Summary $summary -Level "WARN" -Message "Could not list RBAC assignments at vault scope: $($roleResult.Error)"
        }
        else {
            $roles = @($roleResult.Data)
            $roleNames = $roles |
                Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_.roleDefinitionName) } |
                Select-Object -ExpandProperty roleDefinitionName -Unique

            if ($roleNames.Count -eq 0) {
                Add-Result -Summary $summary -Level "WARN" -Message "No direct RBAC assignments found for oid '$tokenObjectId' at vault scope (group-based assignment may still apply)."
            }
            else {
                Add-Result -Summary $summary -Level "PASS" -Message "RBAC roles for oid '$tokenObjectId': $($roleNames -join ', ')"
                $hasLikelySecretReadRole = $false
                foreach ($roleName in $roleNames) {
                    if ($secretReadRoleNames -contains $roleName) {
                        $hasLikelySecretReadRole = $true
                        break
                    }
                }

                if ($hasLikelySecretReadRole) {
                    Add-Result -Summary $summary -Level "PASS" -Message "Role assignments include a likely Key Vault secret-read role."
                }
                else {
                    Add-Result -Summary $summary -Level "WARN" -Message "No obvious Key Vault secret-read role found in direct assignments."
                }
            }
        }
    }
    else {
        Add-Result -Summary $summary -Level "WARN" -Message "Skipping RBAC assignment lookup because vault resource id or token object id is unavailable."
    }

    if ($TestSecrets.IsPresent) {
        Write-Section "Secret read tests ($environmentName)"

        $clientIdSecretName = Resolve-PerEnvironmentValue -BaseName "KEYVAULT_CLIENT_ID_SECRET_NAME" -EnvironmentName $environmentName -DotEnvValues $dotEnvValues -DefaultValue "dm-sharepoint-client-id"
        $clientSecretSecretName = Resolve-PerEnvironmentValue -BaseName "KEYVAULT_CLIENT_SECRET_SECRET_NAME" -EnvironmentName $environmentName -DotEnvValues $dotEnvValues -DefaultValue "dm-sharepoint-client-secret"
        $tenantSecretName = Resolve-PerEnvironmentValue -BaseName "KEYVAULT_TENANT_ID_SECRET_NAME" -EnvironmentName $environmentName -DotEnvValues $dotEnvValues -DefaultValue "dm-sharepoint-tenant-id"
        $sqlUserSecretName = Resolve-PerEnvironmentValue -BaseName "KEYVAULT_SQL_USERNAME_SECRET_NAME" -EnvironmentName $environmentName -DotEnvValues $dotEnvValues
        $sqlPasswordSecretName = Resolve-PerEnvironmentValue -BaseName "KEYVAULT_SQL_PASSWORD_SECRET_NAME" -EnvironmentName $environmentName -DotEnvValues $dotEnvValues

        Add-Result -Summary $summary -Level "PASS" -Message "Expected secret names resolved for environment '$environmentName'."
        Write-Host "[INFO][$environmentName] client_id_secret_name      = $clientIdSecretName"
        Write-Host "[INFO][$environmentName] client_secret_secret_name  = $clientSecretSecretName"
        Write-Host "[INFO][$environmentName] tenant_id_secret_name      = $tenantSecretName"
        if (-not [string]::IsNullOrWhiteSpace($sqlUserSecretName)) { Write-Host "[INFO][$environmentName] sql_username_secret_name  = $sqlUserSecretName" }
        if (-not [string]::IsNullOrWhiteSpace($sqlPasswordSecretName)) { Write-Host "[INFO][$environmentName] sql_password_secret_name  = $sqlPasswordSecretName" }

        $secretSpecs = @(
            [pscustomobject]@{ Name = $clientIdSecretName; IsRequired = $true; Label = "SharePoint Client ID" }
            [pscustomobject]@{ Name = $clientSecretSecretName; IsRequired = $true; Label = "SharePoint Client Secret" }
            [pscustomobject]@{ Name = $tenantSecretName; IsRequired = $true; Label = "SharePoint Tenant ID" }
        )

        if (-not [string]::IsNullOrWhiteSpace($sqlUserSecretName)) { $secretSpecs += [pscustomobject]@{ Name = $sqlUserSecretName; IsRequired = $true; Label = "SQL Username" } }
        if (-not [string]::IsNullOrWhiteSpace($sqlPasswordSecretName)) { $secretSpecs += [pscustomobject]@{ Name = $sqlPasswordSecretName; IsRequired = $true; Label = "SQL Password" } }

        foreach ($spec in $secretSpecs) {
            $secretTest = Get-SecretReadResult -VaultName $resolvedVaultName -SecretName $spec.Name -IsRequired $spec.IsRequired -OutputValues $OutputSecretValues.IsPresent
            Add-Result -Summary $summary -Level $secretTest.Level -Message "$($spec.Label): $($secretTest.Message)"
            if ($secretTest.Level -eq "FAIL" -and -not [string]::IsNullOrWhiteSpace($secretTest.Detail)) {
                Write-Host "[DETAIL][$environmentName] $($secretTest.Detail)" -ForegroundColor DarkYellow
            }
        }
    }

    $overallSummaries += $summary
    Write-Host "[SUMMARY][$environmentName] PASS=$($summary.Pass) WARN=$($summary.Warn) FAIL=$($summary.Fail)"
}

Write-Section "Overall summary"
$totalPass = ($overallSummaries | Measure-Object -Property Pass -Sum).Sum
$totalWarn = ($overallSummaries | Measure-Object -Property Warn -Sum).Sum
$totalFail = ($overallSummaries | Measure-Object -Property Fail -Sum).Sum
if ($null -eq $totalPass) { $totalPass = 0 }
if ($null -eq $totalWarn) { $totalWarn = 0 }
if ($null -eq $totalFail) { $totalFail = 0 }

foreach ($row in $overallSummaries) {
    Write-Host "- $($row.Env): PASS=$($row.Pass) WARN=$($row.Warn) FAIL=$($row.Fail)"
}

Write-Host "TOTAL: PASS=$totalPass WARN=$totalWarn FAIL=$totalFail"

if ($totalFail -gt 0) {
    Write-Host "Validation completed with FAILURES." -ForegroundColor Red
    exit 1
}

Write-Host "Validation completed without FAILURES." -ForegroundColor Green
exit 0