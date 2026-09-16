# Create a release candidate, or promote an existing approved patch.
# Creating a candidate never changes the release directive.
# Promotion never builds images, creates a patch, or modifies listing files.
# Neither mode changes distribution or creates/publishes a listing.
#
# Usage:
#   .\release.ps1                          # build, create candidate, render listing
#   .\release.ps1 -SkipBuild               # create candidate from existing .build/
#   .\release.ps1 -PublishRelease -Patch 33 # promote exactly this approved patch
#   .\release.ps1 -Config other.json

[CmdletBinding(DefaultParameterSetName = "Create")]
param(
    [string]$Config = "native-app-config.json",
    [Parameter(Mandatory = $true, ParameterSetName = "Promote")]
    [ValidateRange(0, 2147483647)]
    [int]$Patch,
    [Parameter(ParameterSetName = "Create")]
    [switch]$SkipBuild,
    [Parameter(Mandatory = $true, ParameterSetName = "Promote")]
    [switch]$PublishRelease,
    [Parameter(ParameterSetName = "Create")]
    [switch]$SkipGitCheck
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$rootDir = Split-Path -Parent $scriptDir
$configPath = if ([System.IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $scriptDir $Config }
$buildDir = Join-Path $rootDir ".build"
$pkg = "MENDIX_SPCS_PKG"

if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "Config not found: $configPath (copy native-app-config.example.json and fill it in)."
}
$cfg = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$conn = $cfg.snowConnection
$version = $cfg.version
if ([string]::IsNullOrWhiteSpace($conn)) { throw "Config is missing 'snowConnection'." }
# Version is interpolated as an unquoted SQL identifier during promotion.
if ($version -notmatch '^[A-Za-z_][A-Za-z0-9_$]*$') {
    throw "Config 'version' must be an unquoted Snowflake identifier, for example v1."
}

function Expand-SnowRows($Value) {
    if ($Value -is [System.Array]) {
        foreach ($item in $Value) { Expand-SnowRows $item }
    } elseif ($Value -is [System.Management.Automation.PSCustomObject]) {
        $Value
    } else {
        throw "Expected Snowflake JSON result rows."
    }
}

function Invoke-SnowQuery([string]$Query) {
    $json = & snow sql -q $Query --connection $conn --format json
    if ($LASTEXITCODE -ne 0) { throw "Snowflake query failed: $Query" }
    $raw = $json -join "`n"
    if ([string]::IsNullOrWhiteSpace($raw)) { throw "Snowflake returned empty output: $Query" }
    try { $parsed = ConvertFrom-Json -InputObject $raw -ErrorAction Stop }
    catch { throw "Snowflake returned invalid JSON: $Query" }
    # CLI versions can wrap the rows in an outer array of statement results.
    if ($null -ne $parsed) { Expand-SnowRows $parsed }
    elseif ($raw.Trim() -ne '[]') { throw "Snowflake returned no JSON result rows: $Query" }
}

if ($PSCmdlet.ParameterSetName -eq "Promote") {
    if (-not $PublishRelease) { throw "Promotion requires -PublishRelease." }
    $packages = @(Invoke-SnowQuery "SHOW APPLICATION PACKAGES LIKE '$pkg'" |
        Where-Object { $_.name -eq $pkg })
    if ($packages.Count -ne 1 -or $packages[0].distribution -ne "EXTERNAL") {
        throw "Promotion requires the live package $pkg to have distribution EXTERNAL."
    }
    $matches = @(Invoke-SnowQuery "SHOW VERSIONS IN APPLICATION PACKAGE $pkg" |
        Where-Object { $_.version -eq $version -and [string]$_.patch -eq [string]$Patch -and -not $_.dropped_on })
    if ($matches.Count -ne 1) { throw "Existing $version patch $Patch was not found uniquely in $pkg." }
    $candidate = $matches[0]
    if ($candidate.review_status -ne "APPROVED") {
        throw "$version patch $Patch is not APPROVED (review_status=$($candidate.review_status))."
    }
    $channels = @(([string]$candidate.release_channel_names -split ',') | ForEach-Object { $_.Trim() })
    if ($candidate.state -ne "READY" -or $channels -notcontains "DEFAULT") {
        throw "$version patch $Patch must be READY and available in the DEFAULT release channel."
    }
    Write-Host "Promoting $version patch $Patch in $pkg..." -ForegroundColor Cyan
    Invoke-SnowQuery "ALTER APPLICATION PACKAGE $pkg MODIFY RELEASE CHANNEL DEFAULT SET DEFAULT RELEASE DIRECTIVE VERSION = $version PATCH = $Patch" | Out-Null
    Write-Host "Default release directive -> $version PATCH $Patch." -ForegroundColor Green
    return
}

# Snapshot existing patches so the summary cannot mistake a default patch 0 or
# another release for the candidate just created. Concurrent creates are ambiguous.
$before = @(Invoke-SnowQuery "SHOW VERSIONS IN APPLICATION PACKAGE $pkg" |
    Where-Object { $_.version -eq $version })

if (-not $SkipBuild) {
    Write-Host "[1/4] Building and pushing images..." -ForegroundColor Cyan
    & (Join-Path $scriptDir "build-and-push.ps1") -Config $configPath
    if ($LASTEXITCODE -ne 0) { throw "build-and-push failed." }
} else {
    Write-Host "[1/4] Reusing existing images and .build/." -ForegroundColor DarkGray
}
if (-not (Test-Path -LiteralPath (Join-Path $buildDir "snowflake.yml") -PathType Leaf)) {
    throw "$buildDir/snowflake.yml not found; run without -SkipBuild first."
}

Write-Host "[2/4] Creating the next patch of $version..." -ForegroundColor Cyan
$createArgs = @("app", "version", "create", $version, "--connection", $conn, "--force", "--no-interactive")
if ($SkipGitCheck) { $createArgs += "--skip-git-check" }
Push-Location $buildDir
try {
    & snow @createArgs
    if ($LASTEXITCODE -ne 0) { throw "snow app version create failed; no release directive was changed." }
} finally {
    Pop-Location
}

Write-Host "[3/4] Checking the created patch..." -ForegroundColor Cyan
$after = @(Invoke-SnowQuery "SHOW VERSIONS IN APPLICATION PACKAGE $pkg" |
    Where-Object { $_.version -eq $version -and -not $_.dropped_on })
$previousPatches = @($before | ForEach-Object { [string]$_.patch })
$created = @($after | Where-Object { $previousPatches -notcontains [string]$_.patch })
if ($created.Count -ne 1 -or [string]$created[0].patch -notmatch '^\d+$') {
    throw "Could not identify exactly one new patch. Check SHOW VERSIONS before retrying; creation may have succeeded. No release directive was changed."
}
$createdPatch = $created[0].patch
Write-Host "Created $version patch $createdPatch; review_status=$($created[0].review_status)." -ForegroundColor Green

Write-Host "[4/4] Rendering listing manifest..." -ForegroundColor Cyan
$tmpl = Join-Path $rootDir "listing/listing-manifest.template.yml"
$out = Join-Path $rootDir "listing/listing-manifest.yml"
if (Test-Path -LiteralPath $tmpl -PathType Leaf) {
    $targets = @($cfg.consumerTargets)
    $accountsYaml = ConvertTo-Json -InputObject $targets -Compress
    $text = (Get-Content -LiteralPath $tmpl -Raw).Replace("<CONSUMER_ACCOUNTS>", $accountsYaml)
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($out, $text, $utf8NoBom)
    Write-Host "Rendered: $out" -ForegroundColor DarkGray
} else {
    Write-Host "Template not found ($tmpl); skipping render." -ForegroundColor Yellow
}
Write-Host "Validate this frozen patch and wait for security approval before promotion."
Write-Host "Promote separately with the same config: release.ps1 -PublishRelease -Patch $createdPatch"
Write-Host "Listing creation, editing, and publication are separate operations."
