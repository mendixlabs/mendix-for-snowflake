# setup.ps1 - Provision the Mendix Deployment Admin UI on SPCS
#
# Usage:
#   .\setup.ps1
#   .\setup.ps1 -Config "other-config.json"
#
# Prerequisites:
#   - Controller already deployed (Controller/setup.ps1 has been run)
#   - Admin UI image built and pushed (.\build-and-push.ps1)
#   - Connection's default role can CREATE SERVICE in the schema (ACCOUNTADMIN
#     in the default setup so the admin UI shares ownership with the controller)

param(
    [string]$Config = "admin-ui-config.json"
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
$ctrlSvc  = $cfg.snowflake.controllerService
$ctrlPort = $cfg.snowflake.controllerPort
$opRole   = $cfg.access.operatorRole

# Snowflake internal DNS: service name lowercased, underscores replaced with dashes.
$ctrlDns       = ($ctrlSvc.ToLower() -replace "_", "-")
$controllerUrl = "http://${ctrlDns}:${ctrlPort}"

function Run-Sql {
    param([string]$Sql, [string]$Label = "")
    if ($Label) { Write-Host "  $Label..." -ForegroundColor DarkGray }
    $tmpFile = [System.IO.Path]::GetTempFileName() + ".sql"
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($tmpFile, $Sql, $utf8NoBom)
    try {
        $out = cmd /c "snow sql -f `"$tmpFile`" --connection $conn --enable-templating NONE 2>&1"
        if ($LASTEXITCODE -ne 0) { Write-Error "SQL failed ($Label):`n$out" }
    } finally {
        Remove-Item $tmpFile -Force -ErrorAction SilentlyContinue
    }
}

# ---- step 1: operator role ----------------------------------------------------

Write-Host "[1/3] Operator role..." -ForegroundColor Cyan

Run-Sql @"
CREATE ROLE IF NOT EXISTS $opRole;
"@ -Label "create $opRole"

# ---- step 2: admin UI service + endpoint grant -------------------------------

Write-Host "[2/3] Admin UI service..." -ForegroundColor Cyan

$serviceSpec = @"
spec:
  containers:
  - name: streamlit
    image: /$repo/mendix-admin-ui:latest
    env:
      CONTROLLER_URL: $controllerUrl
      STREAMLIT_SERVER_MAX_UPLOAD_SIZE: "1024"
      # iX Classic dark palette. Behind SPCS the app runs embedded, which defaults
      # Streamlit to light regardless of OS, so we pin base=dark and supply the full
      # palette here. This themes Streamlit's own widgets (branding.py only adds the
      # font + Siemens Deep Blue logo backdrop). Keep in sync with update.ps1.
      STREAMLIT_THEME_BASE: "dark"
      STREAMLIT_THEME_PRIMARY_COLOR: "#00bde3"
      STREAMLIT_THEME_BACKGROUND_COLOR: "#0f1619"
      STREAMLIT_THEME_SECONDARY_BACKGROUND_COLOR: "#283236"
      STREAMLIT_THEME_TEXT_COLOR: "#f5fcff"
    readinessProbe:
      port: 8501
      path: /_stcore/health
  endpoints:
  - name: streamlit
    port: 8501
    public: true
capabilities:
  securityContext:
    executeAsCaller: true
"@

# executeAsCaller requires a caller-token validity; without it the role-resolution
# session fails with OAUTH_ACCESS_TOKEN_EXPIRED.
Run-Sql @"
CREATE SERVICE IF NOT EXISTS $dbSchema.MENDIX_DEPLOY_ADMIN_UI
    IN COMPUTE POOL $pool
    FROM SPECIFICATION `$`$
$serviceSpec
`$`$
    MIN_INSTANCES = 1
    MAX_INSTANCES = 1
    QUERY_WAREHOUSE = $wh;

ALTER SERVICE $dbSchema.MENDIX_DEPLOY_ADMIN_UI SET SERVICE_CALLER_TOKEN_VALIDITY_SECS = 1800;

GRANT SERVICE ROLE $dbSchema.MENDIX_DEPLOY_ADMIN_UI!ALL_ENDPOINTS_USAGE
    TO ROLE $opRole;
"@ -Label "CREATE SERVICE + endpoint grant"

# ---- step 3: endpoint ---------------------------------------------------------

Write-Host "[3/3] Waiting for endpoint..." -ForegroundColor Cyan

$deadline = (Get-Date).AddMinutes(5)
$url = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 15
    # & snow ... | Out-String — avoid `cmd /c ... 2>&1`, which on PowerShell 5.1
    # wraps each output line into an ErrorRecord and breaks multi-line regex.
    $raw = (& snow sql -q "SHOW ENDPOINTS IN SERVICE $dbSchema.MENDIX_DEPLOY_ADMIN_UI;" --connection $conn --format json --enable-templating NONE) | Out-String
    if ($raw -match '"ingress_url"\s*:\s*"([^"]+)"') {
        $candidate = $Matches[1]
        if ($candidate -notlike "*provisioning*") {
            $url = if ($candidate -match '^https?://') { $candidate } else { "https://$candidate" }
            break
        }
    }
}

Write-Host ""
Write-Host "Done!" -ForegroundColor Green
Write-Host "  Service:       $dbSchema.MENDIX_DEPLOY_ADMIN_UI"
Write-Host "  Operator role: $opRole" -ForegroundColor DarkGray
Write-Host "                 Grant to humans who should access the UI:" -ForegroundColor DarkGray
Write-Host "                   GRANT ROLE $opRole TO USER <username>;" -ForegroundColor DarkGray
if ($url) {
    Write-Host "  Endpoint:      $url" -ForegroundColor Yellow
} else {
    Write-Host "  Endpoint:      still provisioning - run: SHOW ENDPOINTS IN SERVICE $dbSchema.MENDIX_DEPLOY_ADMIN_UI;" -ForegroundColor DarkGray
}
