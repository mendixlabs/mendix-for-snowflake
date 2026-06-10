# deploy.ps1 - Deploy a Mendix PAD package to Snowpark Container Services
#
# Usage:
#   .\deploy.ps1 -PadPath "C:\path\to\MyApp_portable.zip"
#   .\deploy.ps1                              # prompts for path
#   .\deploy.ps1 -Config "my-config.json"     # use a different config file
#
# Configuration:
#   All settings are read from deploy-config.json (or the file specified by -Config).
#   Copy deploy-config.json, fill in your values, and keep it alongside this script.
#
# Prerequisites:
#   - Rancher Desktop (or Docker Desktop) running with dockerd engine
#   - Docker logged in to the Snowflake registry
#   - Snowflake CLI (`snow`) installed and connection configured

param(
    [string]$PadPath,
    [string]$Config = "deploy-config.json"
)

$ErrorActionPreference = "Stop"
$cleanupPath = $null

# ============================================================
# Load configuration
# ============================================================
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = if ([System.IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $scriptDir $Config }

if (-not (Test-Path $configPath)) {
    Write-Error "Config file not found: $configPath`nCopy deploy-config.json and fill in your values."
    exit 1
}

$cfg = Get-Content $configPath -Raw | ConvertFrom-Json

# Extract config values
$SnowConnection = $cfg.snowConnection
$ServiceName    = $cfg.service.name
$ImageRepo      = $cfg.service.imageRepo
$ImageName      = $cfg.service.imageName
$EAI            = $cfg.service.externalAccessIntegration

$DbHost         = $cfg.database.host
$DbPort         = $cfg.database.port
$DbName         = $cfg.database.name
$DbUser         = $cfg.database.username
$DbPass         = $cfg.database.password
$DbSsl          = $cfg.database.useSsl

$AdminPass      = $cfg.mendix.adminPassword
$FileStage      = $cfg.mendix.fileStorageStage

$MemRequest     = $cfg.resources.memory.request
$MemLimit       = $cfg.resources.memory.limit
$CpuRequest     = $cfg.resources.cpu.request
$CpuLimit       = $cfg.resources.cpu.limit

# Derive registry host from snow CLI connection
Write-Host "Loading registry URL from snow CLI..." -ForegroundColor DarkGray
$registryRaw = snow spcs image-registry url --connection $SnowConnection 2>$null
$RegistryHost = ($registryRaw | Out-String).Trim()
if (-not $RegistryHost) {
    Write-Error "Could not determine registry URL from snow CLI connection '$SnowConnection'"
    exit 1
}

Write-Host "  Registry: $RegistryHost" -ForegroundColor DarkGray
Write-Host "  Service:  $ServiceName" -ForegroundColor DarkGray
Write-Host ""

# ============================================================
# Deploy
# ============================================================
try {

# Prompt for PAD path if not provided
if (-not $PadPath) {
    $PadPath = Read-Host "Enter path to PAD zip file or extracted folder"
}

# Resolve to absolute path
$PadPath = Resolve-Path $PadPath

# Determine if it's a ZIP or a directory
if (Test-Path $PadPath -PathType Leaf) {
    if ($PadPath -notmatch '\.zip$') {
        Write-Error "File must be a .zip archive: $PadPath"
        exit 1
    }

    Write-Host "[1/5] Extracting PAD package..." -ForegroundColor Cyan
    $ExtractDir = Join-Path $env:TEMP "mendix-pad-build-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    $cleanupPath = $ExtractDir
    Expand-Archive -Path $PadPath -DestinationPath $ExtractDir -Force

    $children = Get-ChildItem $ExtractDir
    if ($children.Count -eq 1 -and $children[0].PSIsContainer) {
        $BuildContext = $children[0].FullName
    } else {
        $BuildContext = $ExtractDir
    }
} elseif (Test-Path $PadPath -PathType Container) {
    Write-Host "[1/5] Using existing folder (no extraction needed)" -ForegroundColor Cyan
    $BuildContext = $PadPath
} else {
    Write-Error "Path not found: $PadPath"
    exit 1
}

# Verify it looks like a PAD package
if (-not (Test-Path "$BuildContext\bin\start")) {
    Write-Error "Not a valid PAD package: missing bin/start in $BuildContext"
    exit 1
}

# Create Dockerfile if not present
$DockerfilePath = "$BuildContext\Dockerfile"
if (-not (Test-Path $DockerfilePath)) {
    Write-Host "  Creating Dockerfile..." -ForegroundColor DarkGray
    @"
FROM eclipse-temurin:21-jdk
WORKDIR /mendix
COPY ./app ./app
COPY ./bin ./bin
COPY ./etc ./etc
COPY ./lib ./lib
ENV MX_LOG_LEVEL=info
EXPOSE 8080 8090
CMD ["./bin/start", "etc/Default"]
"@ | Set-Content -Path $DockerfilePath -Encoding UTF8
}

# Build
Write-Host "[2/5] Building Docker image..." -ForegroundColor Cyan
$tag = "${ImageName}:$(Get-Date -Format 'yyyyMMdd-HHmm')"
docker build --platform linux/amd64 -t $tag "$BuildContext"
if ($LASTEXITCODE -ne 0) { Write-Error "Docker build failed" }

# Tag and push
Write-Host "[3/5] Pushing to Snowflake registry..." -ForegroundColor Cyan
$fullTag = "$RegistryHost/$ImageRepo/${ImageName}:latest"
docker tag $tag $fullTag
docker push $fullTag
if ($LASTEXITCODE -ne 0) { Write-Error "Docker push failed. Are you logged in? Run: docker login $RegistryHost" }

# Update service spec (forces fresh image pull)
Write-Host "[4/5] Updating service (ALTER SERVICE FROM SPECIFICATION)..." -ForegroundColor Cyan
$sslValue = if ($DbSsl) { "true" } else { "false" }
$spec = @"
spec:
  containers:
  - name: mendix-app
    image: /$ImageRepo/${ImageName}:latest
    env:
      RUNTIME_PARAMS_DATABASETYPE: "POSTGRESQL"
      RUNTIME_PARAMS_DATABASEHOST: "${DbHost}:${DbPort}"
      RUNTIME_PARAMS_DATABASENAME: "$DbName"
      RUNTIME_PARAMS_DATABASEUSERNAME: "$DbUser"
      RUNTIME_PARAMS_DATABASEPASSWORD: "$DbPass"
      RUNTIME_PARAMS_DATABASEUSESSL: "$sslValue"
      RUNTIME_PARAMS_COM_MENDIX_CORE_STORAGESERVICE: "com.mendix.storage.localfilesystem"
      RUNTIME_PARAMS_UPLOADEDFILESPATH: "/mnt/filestorage"
      M2EE_ADMIN_PASS: "$AdminPass"
      RUNTIME_ADMINUSER_PASSWORD: "$AdminPass"
    readinessProbe:
      port: 8080
      path: /
    resources:
      requests:
        memory: $MemRequest
        cpu: $CpuRequest
      limits:
        memory: $MemLimit
        cpu: $CpuLimit
    volumeMounts:
    - name: filestorage
      mountPath: /mnt/filestorage
  volumes:
  - name: filestorage
    source: stage
    stageConfig:
      name: "$FileStage"
  endpoints:
  - name: mendix-web
    port: 8080
    public: true
  logExporters:
    eventTableConfig:
      logLevel: INFO
"@

$env:SNOW_LOG = "CRITICAL"
$alterSql = "ALTER SERVICE $ServiceName FROM SPECIFICATION `$`$$spec`$`$;"
snow sql -q $alterSql --connection $SnowConnection --format json 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ALTER SERVICE failed. Check that your service exists and EAI is attached." -ForegroundColor Red
    Write-Host "  Attempting suspend/resume as fallback (note: this may not pick up the new image)..." -ForegroundColor DarkYellow
    snow sql -q "ALTER SERVICE $ServiceName SUSPEND;" --connection $SnowConnection --format json 2>&1 | Out-Null
    Start-Sleep -Seconds 5
    snow sql -q "ALTER SERVICE $ServiceName RESUME;" --connection $SnowConnection --format json 2>&1 | Out-Null
}

# Get endpoint URL
Write-Host "[5/5] Deployed!" -ForegroundColor Green
try {
    $endpointsRaw = snow sql -q "SHOW ENDPOINTS IN SERVICE $ServiceName;" --connection $SnowConnection --format json 2>$null
    $endpointsJson = ($endpointsRaw | Out-String).Trim()
    if ($endpointsJson -match '"ingress_url"\s*:\s*"([^"]+)"') {
        $ingressUrl = $Matches[1]
    }
} catch {
    $ingressUrl = $null
}

Write-Host ""
Write-Host "  Image: $fullTag" -ForegroundColor DarkGray
Write-Host "  Service will be ready in ~2-3 minutes." -ForegroundColor DarkGray
if ($ingressUrl) {
    Write-Host "  URL: https://$ingressUrl" -ForegroundColor Yellow
} else {
    Write-Host "  URL: (provisioning - check with SHOW ENDPOINTS)" -ForegroundColor DarkGray
}

} finally {
    if ($cleanupPath -and (Test-Path $cleanupPath)) {
        Write-Host "  Cleaning up temp files..." -ForegroundColor DarkGray
        Remove-Item -Recurse -Force $cleanupPath
    }
}
