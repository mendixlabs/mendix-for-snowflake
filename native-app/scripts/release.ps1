# release.ps1 - Cut a versioned release of the Mendix-on-SPCS Native App.
#
# Pipeline (Phase 5a, actionable now):
#   1. build-and-push.ps1   -> rebuild + push the three images, regenerate .build/
#   2. snow app version create <version>  (from the token-resolved .build/)
#   3. SHOW VERSIONS         -> print patch + review_status for the package
#   4. render listing-manifest.template.yml -> listing-manifest.yml (tokens resolved)
#
# Gated tail (Phase 5b, EXTERNAL distribution only):
#   5. set the DEFAULT release directive to <version> PATCH <patch>, but ONLY when
#      distribution=EXTERNAL AND review_status=APPROVED AND -PublishRelease is passed.
#
# This script never flips DISTRIBUTION=EXTERNAL and never runs CREATE EXTERNAL
# LISTING (both are irreversible / ToS-gated / NAAAPS-gated). It prints those
# commands for an operator to run deliberately. See HOW-TO-PUBLISH.md.
#
# Guardrails: only ever touches MENDIX_SPCS_* objects; reads no secrets, prints none.
#
# Usage:
#   .\release.ps1                       # build + version create + show + render manifest
#   .\release.ps1 -SkipBuild            # skip the image rebuild (version-create only)
#   .\release.ps1 -Patch 1              # cut/track a specific patch number
#   .\release.ps1 -PublishRelease       # also set the release directive (EXTERNAL+APPROVED only)
#   .\release.ps1 -Config other.json

param(
    [string]$Config = "native-app-config.json",
    [int]$Patch = 0,
    [switch]$SkipBuild,
    [switch]$PublishRelease,
    [switch]$SkipGitCheck
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$rootDir   = Split-Path -Parent $scriptDir                       # native-app/
$configPath = if ([System.IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $scriptDir $Config }

if (-not (Test-Path $configPath)) {
    Write-Error "Config not found: $configPath (copy native-app-config.example.json and fill it in)"
    exit 1
}

$cfg          = Get-Content $configPath -Raw | ConvertFrom-Json
$conn         = $cfg.snowConnection
$version      = $cfg.version
$distribution = if ($cfg.distribution) { $cfg.distribution } else { "INTERNAL" }
$listingName  = $cfg.listingName
$targets      = @($cfg.consumerTargets)
$pkg          = "MENDIX_SPCS_PKG"
$buildDir     = Join-Path $rootDir ".build"

if ([string]::IsNullOrWhiteSpace($version)) {
    Write-Error "Config is missing 'version' (e.g. \"v1\")."
    exit 1
}

# ---- 1. build + push images, regenerate token-resolved .build/ ---------------
if (-not $SkipBuild) {
    Write-Host "[1/4] Building and pushing images..." -ForegroundColor Cyan
    & (Join-Path $scriptDir "build-and-push.ps1") -Config $Config
    if ($LASTEXITCODE -ne 0) { Write-Error "build-and-push failed."; exit 1 }
} else {
    Write-Host "[1/4] -SkipBuild: reusing existing images and .build/." -ForegroundColor DarkGray
    if (-not (Test-Path $buildDir)) {
        Write-Error "$buildDir not found; run without -SkipBuild first."
        exit 1
    }
}

# ---- 2. version create (from the token-resolved .build/) ---------------------
# The version freezes the image digests currently in the repo. snow reads the
# version name from .build/app/manifest.yml; --patch is omitted for a brand-new
# version (-> patch 0) and only passed when explicitly tracking an existing patch.
Write-Host "[2/4] Creating version $version (patch target $Patch)..." -ForegroundColor Cyan
$createArgs = @("app", "version", "create", $version, "--connection", $conn, "--force")
if ($SkipGitCheck) { $createArgs += "--skip-git-check" }
Push-Location $buildDir
try {
    & snow @createArgs
    $createExit = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($createExit -ne 0) {
    Write-Error "snow app version create failed (if it blocked on a git check, re-run with -SkipGitCheck)."
    exit 1
}

# ---- 3. show versions + review_status ----------------------------------------
Write-Host "[3/4] Versions in $pkg :" -ForegroundColor Cyan
$showJson = & snow sql -q "SHOW VERSIONS IN APPLICATION PACKAGE $pkg" --connection $conn --format json
$rows = $null
try { $rows = $showJson | ConvertFrom-Json } catch {}
$reviewStatus = $null
if ($rows) {
    foreach ($r in $rows) {
        # column casing varies; normalise to a hashtable lookup
        $h = @{}
        $r.PSObject.Properties | ForEach-Object { $h[$_.Name.ToLower()] = $_.Value }
        $vname = $h["version"]
        $vpatch = $h["patch"]
        $vreview = $h["review_status"]
        Write-Host ("  {0} patch {1}  review_status={2}" -f $vname, $vpatch, $vreview)
        if ($vname -eq $version -and [string]$vpatch -eq [string]$Patch) { $reviewStatus = $vreview }
    }
} else {
    Write-Host $showJson
}

# ---- 4. render the listing manifest (tokens resolved) ------------------------
# Reuses the build-and-push token-substitution approach (string .Replace + UTF-8
# no-BOM write). Output is gitignored because it carries real consumer locators.
Write-Host "[4/4] Rendering listing manifest..." -ForegroundColor Cyan
$tmpl = Join-Path $rootDir "listing/listing-manifest.template.yml"
$out  = Join-Path $rootDir "listing/listing-manifest.yml"
if (Test-Path $tmpl) {
    $accountsYaml = "[" + (($targets | ForEach-Object { '"' + $_ + '"' }) -join ", ") + "]"
    $text = (Get-Content $tmpl -Raw).Replace("<CONSUMER_ACCOUNTS>", $accountsYaml)
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($out, $text, $utf8NoBom)
    Write-Host "  Rendered: $out  (targets: $accountsYaml)" -ForegroundColor DarkGray
} else {
    Write-Host "  Template not found ($tmpl); skipping render." -ForegroundColor Yellow
}

# ---- 5. release directive (GATED) --------------------------------------------
Write-Host ""
if ($distribution -ne "EXTERNAL") {
    Write-Host "distribution=${distribution}: INTERNAL package has no NAAAPS gate and no consumer listing." -ForegroundColor Green
    Write-Host "Version $version patch $Patch is cut. Install dry-run: CREATE APPLICATION ... USING VERSION $version PATCH $Patch;"
} elseif (-not $PublishRelease) {
    Write-Host "distribution=EXTERNAL but -PublishRelease not passed; not setting a release directive." -ForegroundColor Yellow
    Write-Host "To publish: flip distribution, wait for NAAAPS APPROVED + provider approval, then re-run with -PublishRelease."
} elseif ($reviewStatus -ne "APPROVED") {
    Write-Host "review_status for $version patch ${Patch} is '$reviewStatus' (not APPROVED)." -ForegroundColor Yellow
    Write-Host "Release directive NOT set. NAAAPS must reach APPROVED first (see plan section 9, R1)."
} else {
    Write-Host "review_status=APPROVED and -PublishRelease set: setting DEFAULT release directive." -ForegroundColor Cyan
    & snow app release-directive set default --version $version --patch $Patch --connection $conn
    if ($LASTEXITCODE -ne 0) { Write-Error "release-directive set failed."; exit 1 }
    Write-Host "Default release directive -> $version PATCH $Patch." -ForegroundColor Green
}

# ---- operator next-steps (never executed here) -------------------------------
if ($distribution -eq "EXTERNAL") {
    Write-Host ""
    Write-Host "Manual, irreversible, do deliberately when the business is ready to publish:" -ForegroundColor Magenta
    Write-Host "  ALTER APPLICATION PACKAGE $pkg SET DISTRIBUTION = EXTERNAL;   -- accept ToS, starts NAAAPS"
    Write-Host "  -- then, after APPROVED + provider approval and -PublishRelease above:"
    Write-Host "  CREATE EXTERNAL LISTING $listingName"
    Write-Host "    APPLICATION PACKAGE $pkg"
    Write-Host "    AS '<paste native-app/listing/listing-manifest.yml>'"
    Write-Host "    PUBLISH = FALSE REVIEW = TRUE;"
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
