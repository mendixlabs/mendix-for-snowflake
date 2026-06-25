# build-and-push.ps1 - Build the three Mendix images, push them to the application
# package's image repository, and stage a deploy copy with the provider FQN tokens
# resolved.
#
# Replaces the per-component build-and-push scripts; pushes to the native-app image
# repository (snowflake.yml entity `images`) instead of the ad-hoc POC_REPO.
#
# Usage:
#   .\build-and-push.ps1
#   .\build-and-push.ps1 -Config "other-config.json"
#
# Prerequisites:
#   - Docker running (Rancher/Docker Desktop)
#   - Snowflake CLI configured; image repository created
#       (snow spcs image-repository deploy images -c <conn>)
#
# native-app-config.json (gitignored) holds the provider-specific values so the
# personal dev DB name never lands in git:
#   {
#     "snowConnection": "<conn>",
#     "providerDb":     "<DB>",
#     "providerSchema": "<SCHEMA>",
#     "repo":           "MENDIX_NATIVE_REPO"
#   }

param(
    [string]$Config = "native-app-config.json"
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$rootDir   = Split-Path -Parent $scriptDir                       # native-app/
$repoRoot  = Split-Path -Parent $rootDir                         # project root
$configPath = if ([System.IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $scriptDir $Config }

if (-not (Test-Path $configPath)) {
    Write-Error "Config not found: $configPath (copy native-app-config.example.json and fill it in)"
    exit 1
}

$cfg     = Get-Content $configPath -Raw | ConvertFrom-Json
$conn    = $cfg.snowConnection
$pdb     = $cfg.providerDb
$pschema = $cfg.providerSchema
$repo    = $cfg.repo
$repoFqn = "$pdb/$pschema/$repo"

# image-name -> source build context (relative to the project root)
$images = @{
    "mendix-deploy-controller" = "Controller"
    "mendix-admin-ui"          = "Admin UI"
    "mendix-base"              = "Mendix Base Image"
}

Write-Host "[1/3] Logging into Snowflake image registry..." -ForegroundColor Cyan
& snow spcs image-registry login --connection $conn
if ($LASTEXITCODE -ne 0) { Write-Error "Registry login failed."; exit 1 }

$registry = (& snow spcs image-registry url --connection $conn).Trim()
# Docker repository references must be lowercase; the Snowflake registry is
# case-insensitive and stores the path lowercased. The manifest/spec image paths
# keep the SQL identifier casing (Snowflake resolves them case-insensitively).
$repoUrl  = "$registry/$repoFqn".ToLower()

Write-Host "[2/3] Building and pushing images to $repoUrl ..." -ForegroundColor Cyan
foreach ($name in $images.Keys) {
    $context = Join-Path $repoRoot $images[$name]
    Write-Host "  $name  (context: $($images[$name]))" -ForegroundColor DarkGray
    # SPCS requires a single linux/amd64 image. --provenance=false suppresses the
    # BuildKit attestation/provenance manifests that otherwise make the push an OCI
    # manifest list, which SPCS can fail to resolve at CREATE SERVICE time.
    & docker build --platform linux/amd64 --provenance=false -t $name $context
    if ($LASTEXITCODE -ne 0) { Write-Error "Build failed: $name"; exit 1 }
    & docker tag $name "$repoUrl/${name}:latest"
    & docker push "$repoUrl/${name}:latest"
    if ($LASTEXITCODE -ne 0) { Write-Error "Push failed: $name"; exit 1 }
}

# ---- stage a deploy copy with tokens resolved --------------------------------
# The committed app/ keeps <PROVIDER_*> tokens so the personal DB name stays out
# of git. Resolve them into a gitignored .build/ copy that `snow app` deploys.
Write-Host "[3/3] Staging deploy copy with provider FQN resolved..." -ForegroundColor Cyan
$buildDir = Join-Path $rootDir ".build"
if (Test-Path $buildDir) { Remove-Item $buildDir -Recurse -Force }
Copy-Item (Join-Path $rootDir "app") (Join-Path $buildDir "app") -Recurse
Copy-Item (Join-Path $rootDir "snowflake.yml") (Join-Path $buildDir "snowflake.yml")

foreach ($f in @("app/manifest.yml", "app/setup_script.sql")) {
    $p = Join-Path $buildDir $f
    $text = Get-Content $p -Raw
    $text = $text.Replace("<PROVIDER_DB>", $pdb).Replace("<PROVIDER_SCHEMA>", $pschema).Replace("<REPO>", $repo)
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($p, $text, $utf8NoBom)
}

Write-Host ""
Write-Host "Done!" -ForegroundColor Green
Write-Host "  Images:  $repoUrl/{mendix-deploy-controller,mendix-admin-ui,mendix-base}:latest"
Write-Host "  Staged:  $buildDir"
Write-Host "  Deploy:  snow app run -p `"$buildDir`" --connection $conn" -ForegroundColor Yellow
