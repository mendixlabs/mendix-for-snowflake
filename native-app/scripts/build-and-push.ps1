# build-and-push.ps1 - Build the three Mendix images, push them to the application
# package's image repository, and stage a deploy copy with the provider FQN tokens
# resolved.
#
# Pushes to the native-app image repository (snowflake.yml entity `images`).
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
    [string]$Config = "native-app-config.json",
    [string]$SyftPath = "syft"
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$rootDir   = Split-Path -Parent $scriptDir                       # native-app/
$repoRoot  = Split-Path -Parent $rootDir                         # project root
$configPath = if ([System.IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $scriptDir $Config }

# Verify evidence tools before changing the registry.
Get-Command python -ErrorAction Stop | Out-Null
Get-Command $SyftPath -ErrorAction Stop | Out-Null

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
# Capture each pushed image's immutable @sha256 digest so the staged version pins
# to it instead of the mutable :latest tag (security: a frozen app version must
# always run the exact image that passed the NAAAPS review).
$digests = @{}
foreach ($name in $images.Keys) {
    $context = Join-Path $repoRoot $images[$name]
    Write-Host "  $name  (context: $($images[$name]))" -ForegroundColor DarkGray
    # SPCS requires a single linux/amd64 image. --provenance=false suppresses the
    # BuildKit attestation/provenance manifests that otherwise make the push an OCI
    # manifest list, which SPCS can fail to resolve at CREATE SERVICE time.
    & docker build --platform linux/amd64 --provenance=false --build-context "project=$repoRoot" -t $name $context
    if ($LASTEXITCODE -ne 0) { Write-Error "Build failed: $name"; exit 1 }
    & docker tag $name "$repoUrl/${name}:latest"
    & docker push "$repoUrl/${name}:latest"
    if ($LASTEXITCODE -ne 0) { Write-Error "Push failed: $name"; exit 1 }
    # RepoDigests is populated after a successful push: "<repoUrl>/<name>@sha256:...".
    $repoDigest = (& docker inspect --format '{{index .RepoDigests 0}}' "$repoUrl/${name}:latest").Trim()
    if ($repoDigest -match '(@sha256:[0-9a-f]+)$') {
        $digests[$name] = $Matches[1]     # keep the leading "@sha256:..."
        Write-Host "    pinned: $name$($digests[$name])" -ForegroundColor DarkGray
    } else {
        Write-Error "Could not resolve pushed digest for ${name} (docker inspect returned '$repoDigest')."
        exit 1
    }
}

# Capture the exact pushed images, including their immutable registry references.
$evidenceDir = Join-Path $repoRoot ("artifacts/oss/release-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
$evidenceArgs = @((Join-Path $repoRoot "compliance/build_evidence.py"), "--output", $evidenceDir, "--syft", $SyftPath)
foreach ($name in $images.Keys) {
    $evidenceArgs += @("--image", "$name=$repoUrl/$name$($digests[$name])")
}
& python @evidenceArgs
if ($LASTEXITCODE -ne 0) { throw "OSS evidence capture failed. Release staging stopped." }

# ---- stage a deploy copy with tokens resolved --------------------------------
# The committed app/ keeps <PROVIDER_*> tokens so the personal DB name stays out
# of git. Resolve them into a gitignored .build/ copy that `snow app` deploys.
Write-Host "[3/3] Staging deploy copy with provider FQN resolved..." -ForegroundColor Cyan
$buildDir = Join-Path $rootDir ".build"
if (Test-Path $buildDir) { Remove-Item $buildDir -Recurse -Force }
Copy-Item (Join-Path $rootDir "app") (Join-Path $buildDir "app") -Recurse
Copy-Item (Join-Path $rootDir "snowflake.yml") (Join-Path $buildDir "snowflake.yml")
Copy-Item (Join-Path $repoRoot "LICENSE.txt") (Join-Path $buildDir "app/LICENSE.txt")

foreach ($f in @("app/manifest.yml", "app/setup_script.sql")) {
    $p = Join-Path $buildDir $f
    $text = Get-Content $p -Raw
    $text = $text.Replace("<PROVIDER_DB>", $pdb).Replace("<PROVIDER_SCHEMA>", $pschema).Replace("<REPO>", $repo)
    # Pin every "<image>:latest" reference (manifest allow-list, the controller and
    # admin-ui service images, and the controller's MENDIX_BASE_IMAGE env) to the
    # immutable digest just pushed. The committed source keeps :latest for dev; only
    # the frozen .build/ version is digest-pinned. (The IMAGE_REPO env has no :latest
    # and is left as the repo path.)
    foreach ($name in $images.Keys) {
        $text = $text.Replace("${name}:latest", "${name}$($digests[$name])")
    }
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($p, $text, $utf8NoBom)
}

Write-Host ""
Write-Host "Done!" -ForegroundColor Green
Write-Host "  Images:  $repoUrl/{mendix-deploy-controller,mendix-admin-ui,mendix-base}:latest"
Write-Host "  Staged:  $buildDir  (image refs pinned to @sha256 digests)"
Write-Host "  OSS evidence: $evidenceDir (archive with this release; Siemens approval remains separate)"
Write-Host "  Deploy:  snow app run -p `"$buildDir`" --connection $conn" -ForegroundColor Yellow
