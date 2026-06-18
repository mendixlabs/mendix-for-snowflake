# update.ps1 - Refresh the deployed controller service to pick up a new :latest image
#                or a spec change.
#
# Usage:
#   .\update.ps1
#   .\update.ps1 -Config "other-config.json"
#
# Runs ALTER SERVICE ... FROM SPECIFICATION on the controller, which re-resolves
# the image tag to whatever :latest currently points at in the registry. SUSPEND
# /RESUME alone does NOT re-pull a new :latest because SPCS pins the image to a
# sha256 digest at CREATE / ALTER time.
#
# The spec below must stay in sync with the spec in setup.ps1.

param(
    [string]$Config = "controller-config.json"
)

$ErrorActionPreference = "Stop"

$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = if ([System.IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $scriptDir $Config }

if (-not (Test-Path $configPath)) {
    Write-Error "Config not found: $configPath"
    exit 1
}

$cfg  = Get-Content $configPath -Raw | ConvertFrom-Json
$conn = $cfg.snowConnection

$db       = $cfg.snowflake.database
$schema   = $cfg.snowflake.schema
$dbSchema = "$db.$schema"
$pool     = $cfg.snowflake.computePool
$wh       = $cfg.snowflake.warehouse
$repo     = $cfg.snowflake.imageRepo
$eai      = $cfg.snowflake.pgEai

# Keep this spec in sync with setup.ps1.
$serviceSpec = @"
spec:
  containers:
  - name: controller
    image: /$repo/mendix-deploy-controller:latest
    env:
      COMPUTE_POOL: $pool
      IMAGE_REPO: $repo/mendix-base
      DB_SCHEMA: $dbSchema
      PG_EAI: $eai
      QUERY_WAREHOUSE: $wh
    secrets:
    - snowflakeSecret: $dbSchema.CTRL_PG_HOST
      directoryPath: /secrets/pg_host
    - snowflakeSecret: $dbSchema.CTRL_PG_PASS
      directoryPath: /secrets/pg_pass
    readinessProbe:
      port: 8080
      path: /health
    volumeMounts:
    - name: deploy-stage
      mountPath: /mnt/deploy-stage
  volumes:
  - name: deploy-stage
    source: stage
    stageConfig:
      name: "@$dbSchema.MENDIX_DEPLOY_STAGE"
  endpoints:
  - name: controller-api
    port: 8080
    public: true
"@

$sql = @"
ALTER SERVICE $dbSchema.MENDIX_DEPLOY_CONTROLLER FROM SPECIFICATION `$`$
$serviceSpec
`$`$;
"@

$tmpFile = [System.IO.Path]::GetTempFileName() + ".sql"
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($tmpFile, $sql, $utf8NoBom)

Write-Host "ALTER SERVICE $dbSchema.MENDIX_DEPLOY_CONTROLLER ..." -ForegroundColor Cyan
try {
    $out = cmd /c "snow sql -f `"$tmpFile`" --connection $conn --enable-templating NONE 2>&1"
    if ($LASTEXITCODE -ne 0) { Write-Error "ALTER SERVICE failed:`n$out" }
} finally {
    Remove-Item $tmpFile -Force -ErrorAction SilentlyContinue
}

Write-Host "Polling for RUNNING with refreshed digest..." -ForegroundColor Cyan
$deadline = (Get-Date).AddMinutes(5)
$ok = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 10
    # & snow ... | Out-String — avoid `cmd /c ... 2>&1`, which on PowerShell 5.1
    # wraps each output line into an ErrorRecord and breaks multi-line regex.
    $raw = (& snow sql -q "DESCRIBE SERVICE $dbSchema.MENDIX_DEPLOY_CONTROLLER;" --connection $conn --format json --enable-templating NONE) | Out-String
    if ($raw -match '"status"\s*:\s*"RUNNING"') {
        # The spec field is JSON-escaped so quotes appear as \"; match the
        # digest directly without depending on surrounding quote style.
        if ($raw -match '@sha256:([a-f0-9]+)') {
            Write-Host "  RUNNING, digest sha256:$($Matches[1].Substring(0,12))..." -ForegroundColor Green
            $ok = $true
            break
        }
    }
    Write-Host "  still cycling..." -ForegroundColor DarkGray
}

if ($ok) {
    Write-Host ""
    Write-Host "Done!" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "Did not reach RUNNING in 5 min. Check: DESCRIBE SERVICE $dbSchema.MENDIX_DEPLOY_CONTROLLER;" -ForegroundColor Yellow
}
