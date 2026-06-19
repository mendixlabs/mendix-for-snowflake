# upload-pad.ps1 — Deploy a Mendix PAD via the SPCS deployment controller
#
# First deploy (registers the app then uploads the PAD):
#   .\upload-pad.ps1 -AppName manufacturing -PadPath C:\path\to\app.zip `
#     -ControllerUrl https://<ingress>.snowflakecomputing.app -Token "<pat>" `
#     -Config .\controller-config.json `
#     -AppConfig C:\path\to\manufacturing-controller-config.json
#
# Subsequent deploys:
#   .\upload-pad.ps1 -AppName manufacturing -PadPath C:\path\to\app.zip `
#     -ControllerUrl https://<ingress>.snowflakecomputing.app -Token "<pat>" `
#     -Config .\controller-config.json
#
# How it works:
#   Large PADs are uploaded to the Snowflake stage directly via the Snow CLI
#   (avoiding SPCS ingress timeouts), then the controller is told to deploy
#   from the stage via POST /apps/{name}/trigger-deploy.
#
# Token: a Snowflake PAT restricted to MENDIX_DEPLOY_CONTROLLER_ROLE. Generate with:
#   ALTER USER <you> ADD PROGRAMMATIC ACCESS TOKEN <name>
#     ROLE_RESTRICTION = 'MENDIX_DEPLOY_CONTROLLER_ROLE' DAYS_TO_EXPIRY = 90;
#
# AppConfig JSON format (keep outside the repo — contains credentials):
#   {
#     "pg_database":       "manufacturing",
#     "admin_password":    "Admin1234!",
#     "resource_tier":     "medium",
#     "use_caller_rights": true,
#     "owner_role":        "MENDIX_ADMIN_OPERATOR_ROLE",
#     "constants":         { "Module.Constant": "value" }
#   }
#
# owner_role is optional: it sets which operator role sees and manages the app in
# the admin UI. Omit it to default to MENDIX_ADMIN_OPERATOR_ROLE. This deploy PAT's
# role (MENDIX_DEPLOY_CONTROLLER_ROLE) is privileged and can deploy any app.

param(
    [Parameter(Mandatory)][string]$AppName,
    [Parameter(Mandatory)][string]$PadPath,
    [Parameter(Mandatory)][string]$ControllerUrl,
    [Parameter(Mandatory)][string]$Token,
    [Parameter(Mandatory)][string]$Config,
    [string]$AppConfig,
    [int]$TimeoutSeconds = 420
)

$ErrorActionPreference = "Stop"

$PadPath = Resolve-Path $PadPath
if (-not (Test-Path $PadPath -PathType Leaf)) {
    Write-Error "PAD file not found: $PadPath"
    exit 1
}

if (-not (Test-Path $Config)) {
    Write-Error "Config not found: $Config"
    exit 1
}
$cfg        = Get-Content $Config -Raw | ConvertFrom-Json
$conn       = $cfg.snowConnection
$db         = $cfg.snowflake.database
$schema     = $cfg.snowflake.schema
$stageRef   = "@$db.$schema.MENDIX_DEPLOY_STAGE"

$ControllerUrl = $ControllerUrl.TrimEnd("/")
$authHeaders   = @{ Authorization = "Snowflake Token=`"$Token`"" }

# ---- step 1: register (if AppConfig provided) ----------------------------

if ($AppConfig) {
    if (-not (Test-Path $AppConfig)) {
        Write-Error "AppConfig not found: $AppConfig"
        exit 1
    }
    $appCfg = Get-Content $AppConfig -Raw | ConvertFrom-Json

    $body = [ordered]@{
        name              = $AppName
        pg_database       = $appCfg.pg_database
        admin_password    = $appCfg.admin_password
        resource_tier     = if ($appCfg.resource_tier) { $appCfg.resource_tier } else { "medium" }
        use_caller_rights = if ($null -ne $appCfg.use_caller_rights) { [bool]$appCfg.use_caller_rights } else { $false }
        constants         = if ($appCfg.constants) { $appCfg.constants } else { @{} }
    }
    # owner_role controls which operator role sees/manages the app in the admin UI.
    # Omit to let the controller default it to MENDIX_ADMIN_OPERATOR_ROLE.
    if ($appCfg.owner_role) { $body["owner_role"] = $appCfg.owner_role }

    Write-Host "Registering app '$AppName'..." -ForegroundColor Cyan
    try {
        $reg = Invoke-RestMethod -Uri "$ControllerUrl/apps" -Method Post `
            -Headers $authHeaders -ContentType "application/json" `
            -Body ($body | ConvertTo-Json -Depth 5)
        Write-Host "  Registered. Service: $($reg.service_name)" -ForegroundColor DarkGray
    } catch {
        $code = $_.Exception.Response.StatusCode.value__
        if ($code -eq 409) {
            Write-Host "  App already registered, skipping." -ForegroundColor DarkGray
        } else {
            Write-Error "Registration failed ($code): $($_.ErrorDetails.Message)"
            exit 1
        }
    }
}

# ---- step 2: upload PAD to stage -----------------------------------------

$fileName   = [System.IO.Path]::GetFileName($PadPath)
$stageDest  = "$stageRef/apps/$AppName/current.zip"
$sizeMb     = '{0:N1}' -f ((Get-Item $PadPath).Length / 1MB)

Write-Host "Uploading '$fileName' ($sizeMb MB) to stage..." -ForegroundColor Cyan
# Remove existing file first to prevent snow stage copy from nesting the new file inside
# a directory named current.zip (happens when the destination already exists as a file).
& snow stage remove $stageDest --connection $conn 2>$null
& snow stage copy $PadPath $stageDest --connection $conn --overwrite

if ($LASTEXITCODE -ne 0) {
    Write-Error "Stage upload failed."
    exit 1
}
Write-Host "  Stage upload complete." -ForegroundColor DarkGray

# ---- step 3: trigger deploy ----------------------------------------------

Write-Host "Triggering deploy..." -ForegroundColor Cyan
try {
    $response = Invoke-RestMethod -Uri "$ControllerUrl/apps/$AppName/trigger-deploy" -Method Post `
        -Headers $authHeaders -ContentType "application/json" -Body "{}"
    Write-Host "  Accepted: status=$($response.status)" -ForegroundColor DarkGray
} catch {
    $code    = $_.Exception.Response.StatusCode.value__
    $errBody = $_.ErrorDetails.Message
    Write-Error "Trigger failed ($code): $errBody"
    exit 1
}

# ---- step 4: poll --------------------------------------------------------

Write-Host "Polling for status..." -ForegroundColor DarkGray

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 10
    try {
        $s = Invoke-RestMethod -Uri "$ControllerUrl/apps/$AppName" -Method Get `
            -Headers $authHeaders -ErrorAction SilentlyContinue
        Write-Host "  service=$($s.service_status)  deploy=$($s.app.last_deploy_status)" -ForegroundColor DarkGray

        if ($s.app.last_deploy_status -eq "READY") {
            Write-Host ""
            Write-Host "Done!" -ForegroundColor Green
            Write-Host "  App:      $AppName"
            Write-Host "  Endpoint: $($s.app.endpoint_url)" -ForegroundColor Yellow
            exit 0
        }
        if ($s.app.last_deploy_status -eq "FAILED") {
            Write-Error "Deploy failed. Check controller logs."
            exit 1
        }
    } catch {
        Write-Host "  (status check failed, retrying...)" -ForegroundColor DarkGray
    }
}

Write-Error "Timed out after ${TimeoutSeconds}s."
exit 1
