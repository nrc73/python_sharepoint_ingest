param(
    [string]$ContainerName = "smtp4dev-ingest",
    [string]$Image = "rnwood/smtp4dev:latest",
    [int]$HostSmtpPort = 2525,
    [int]$HostWebPort = 5000,
    [int]$WebPortSearchLimit = 50,
    [switch]$KeepExisting
)

function Test-PortAvailable {
    param(
        [int]$Port
    )

    # Prefer OS listener table checks over bind-test, because Docker can reserve
    # 0.0.0.0 bindings that may not be detected by a loopback-only bind probe.
    try {
        if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
            $listeners = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
            if ($listeners) {
                return $false
            }
            return $true
        }
    }
    catch {
        # fallback to netstat below
    }

    $netstatLines = netstat -ano | Select-String -Pattern ":$Port\s+.*LISTENING"
    if ($netstatLines) {
        return $false
    }
    return $true
}

function Resolve-WebPort {
    param(
        [int]$PreferredPort,
        [int]$SearchLimit
    )

    if (Test-PortAvailable -Port $PreferredPort) {
        return $PreferredPort
    }

    for ($candidate = $PreferredPort + 1; $candidate -le ($PreferredPort + $SearchLimit); $candidate++) {
        if (Test-PortAvailable -Port $candidate) {
            Write-Host "Requested web port $PreferredPort is in use. Auto-selected free port: $candidate"
            return $candidate
        }
    }

    throw "Could not find a free web port in range $PreferredPort-$($PreferredPort + $SearchLimit)."
}

Write-Host "Checking Docker availability..."
docker version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Docker is not available. Ensure Docker Desktop is running."
}

if (-not $KeepExisting.IsPresent) {
    Write-Host "Removing existing container (if any): $ContainerName"
    docker rm -f $ContainerName 2>$null | Out-Null
}

$resolvedWebPort = Resolve-WebPort -PreferredPort $HostWebPort -SearchLimit $WebPortSearchLimit

Write-Host "Starting smtp4dev container '$ContainerName'"
docker run -d `
  --name $ContainerName `
  -p "${HostSmtpPort}:25" `
  -p "${resolvedWebPort}:80" `
  $Image | Out-Null

if ($LASTEXITCODE -ne 0) {
    throw "Failed to start smtp4dev container."
}

Write-Host ""
Write-Host "smtp4dev is running."
Write-Host "Web UI            : http://localhost:$resolvedWebPort"
Write-Host "SMTP endpoint     : localhost:$HostSmtpPort (if SQL Server runs on Windows host)"
Write-Host "SMTP endpoint     : host.docker.internal:$HostSmtpPort (if SQL Server runs in Docker container)"
Write-Host "Authentication    : none"
Write-Host "TLS/SSL           : disabled"
Write-Host ""
Write-Host "Use SQL template: sql/configure_local_dbmail_profile.sql"
Write-Host "Then validate Layer 5:"
Write-Host "python sharepoint_setup/dbmail_send_test.py --env dev --profile-name 'Dev Local SMTP' --to 'dev-test@local.invalid'"
