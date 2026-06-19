# setup.ps1 - One-time infrastructure setup for the Mendix SPCS Deployment Controller
#
# Usage:
#   .\setup.ps1
#   .\setup.ps1 -Config "other-config.json"
#
# Prerequisites:
#   - Snowflake CLI (snow) installed and connection configured
#   - ACCOUNTADMIN role on the target account
#   - Docker logged in to the Snowflake registry (for image builds/pushes)

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
$repo     = $cfg.snowflake.imageRepo   # e.g. your_db/public/poc_repo
$eai      = $cfg.snowflake.pgEai

$pgHost = "$($cfg.postgres.host):$($cfg.postgres.port)"
$pgPass = $cfg.postgres.password

# ---- helpers ----------------------------------------------------------------

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

# ---- step 1: role and grants ------------------------------------------------

Write-Host "[1/5] Role and grants..." -ForegroundColor Cyan

Run-Sql @"
CREATE ROLE IF NOT EXISTS MENDIX_DEPLOY_CONTROLLER_ROLE;
GRANT ROLE MENDIX_DEPLOY_CONTROLLER_ROLE TO ROLE ACCOUNTADMIN;
GRANT USAGE ON DATABASE  $db                                     TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
GRANT USAGE ON SCHEMA    $dbSchema                               TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
GRANT USAGE ON WAREHOUSE $wh                                     TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
GRANT USAGE ON INTEGRATION $eai                                  TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
GRANT USAGE            ON COMPUTE POOL $pool                     TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
GRANT READ             ON IMAGE REPOSITORY $dbSchema.POC_REPO    TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
GRANT CREATE SERVICE   ON SCHEMA $dbSchema                       TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
GRANT CREATE SECRET    ON SCHEMA $dbSchema                       TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
GRANT CREATE STAGE     ON SCHEMA $dbSchema                       TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
GRANT CREATE TABLE     ON SCHEMA $dbSchema                       TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
GRANT BIND SERVICE ENDPOINT ON ACCOUNT                           TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
"@ -Label "role + grants"

# ---- step 2: deploy stage and app registry table ----------------------------

Write-Host "[2/5] Stage and table..." -ForegroundColor Cyan

Run-Sql @"
CREATE STAGE IF NOT EXISTS $dbSchema.MENDIX_DEPLOY_STAGE
    ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE');

GRANT READ  ON STAGE $dbSchema.MENDIX_DEPLOY_STAGE TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
GRANT WRITE ON STAGE $dbSchema.MENDIX_DEPLOY_STAGE TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;

CREATE TABLE IF NOT EXISTS $dbSchema.MENDIX_APPS (
    name               VARCHAR    NOT NULL PRIMARY KEY,
    service_name       VARCHAR    NOT NULL,
    pg_database        VARCHAR    NOT NULL,
    resource_tier      VARCHAR    DEFAULT 'medium',
    use_caller_rights  BOOLEAN    DEFAULT FALSE,
    constants          VARIANT,
    pad_stage_path     VARCHAR,
    endpoint_url       VARCHAR,
    last_deploy_status VARCHAR,
    created_at         TIMESTAMP  DEFAULT CURRENT_TIMESTAMP,
    last_deployed_at   TIMESTAMP,
    owner_role         VARCHAR    DEFAULT 'MENDIX_ADMIN_OPERATOR_ROLE'
);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE $dbSchema.MENDIX_APPS
    TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
"@ -Label "stage + table"

# ---- step 3: controller secrets (PG credentials) ----------------------------

Write-Host "[3/5] Secrets..." -ForegroundColor Cyan

$escapedHost = $pgHost -replace "'", "''"
$escapedPass = $pgPass -replace "'", "''"

Run-Sql @"
CREATE OR REPLACE SECRET $dbSchema.CTRL_PG_HOST
    TYPE = GENERIC_STRING SECRET_STRING = '$escapedHost';
CREATE OR REPLACE SECRET $dbSchema.CTRL_PG_PASS
    TYPE = GENERIC_STRING SECRET_STRING = '$escapedPass';
GRANT READ ON SECRET $dbSchema.CTRL_PG_HOST TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
GRANT READ ON SECRET $dbSchema.CTRL_PG_PASS TO ROLE MENDIX_DEPLOY_CONTROLLER_ROLE;
"@ -Label "secrets"

# ---- step 4: controller service ---------------------------------------------

Write-Host "[4/5] Controller service..." -ForegroundColor Cyan

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
capabilities:
  securityContext:
    executeAsCaller: true
"@

# executeAsCaller requires a caller-token validity; without it caller-rights
# sessions fail with OAUTH_ACCESS_TOKEN_EXPIRED.
Run-Sql @"
CREATE SERVICE IF NOT EXISTS $dbSchema.MENDIX_DEPLOY_CONTROLLER
    IN COMPUTE POOL $pool
    FROM SPECIFICATION `$`$
$serviceSpec
`$`$
    MIN_INSTANCES = 1
    MAX_INSTANCES = 1
    QUERY_WAREHOUSE = $wh;

ALTER SERVICE $dbSchema.MENDIX_DEPLOY_CONTROLLER SET SERVICE_CALLER_TOKEN_VALIDITY_SECS = 1800;
"@ -Label "CREATE SERVICE"

# ---- step 5: endpoint -------------------------------------------------------

Write-Host "[5/5] Waiting for endpoint..." -ForegroundColor Cyan

$deadline = (Get-Date).AddMinutes(5)
$url = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 15
    # & snow ... | Out-String — avoid `cmd /c ... 2>&1`, which on PowerShell 5.1
    # wraps each output line into an ErrorRecord and breaks multi-line regex.
    $raw = (& snow sql -q "SHOW ENDPOINTS IN SERVICE $dbSchema.MENDIX_DEPLOY_CONTROLLER;" --connection $conn --format json --enable-templating NONE) | Out-String
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
Write-Host "  Controller: $dbSchema.MENDIX_DEPLOY_CONTROLLER"
if ($url) {
    Write-Host "  Endpoint:   $url" -ForegroundColor Yellow
} else {
    Write-Host "  Endpoint:   still provisioning - run: SHOW ENDPOINTS IN SERVICE $dbSchema.MENDIX_DEPLOY_CONTROLLER;" -ForegroundColor DarkGray
}
