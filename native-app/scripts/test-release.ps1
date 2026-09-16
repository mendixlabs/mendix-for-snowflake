# Offline regression checks. No Snowflake connection or Docker daemon is used.
# Run: powershell -NoProfile -NonInteractive -File native-app/scripts/test-release.ps1
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot 'release.ps1'
$tokens = $null
$parseErrors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile($source, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) { throw ($parseErrors | Out-String) }

$fixtureRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('release-tests-' + [guid]::NewGuid().ToString('N'))
[void](New-Item -ItemType Directory -Path $fixtureRoot)
$passed = 0

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function New-VersionRow([int]$Patch, [string]$Review = 'APPROVED', [string]$Channels = 'DEFAULT') {
    return @{ version = 'V1'; patch = $Patch; review_status = $Review; release_channel_names = $Channels; release_channels = '{"DEFAULT":{"state":"READY","dropped_on":null}}'; dropped_on = $null; state = 'READY' }
}

function Invoke-ReleaseCase {
    param(
        [string]$Name,
        [hashtable]$Parameters,
        [hashtable]$Scenario,
        [bool]$ShouldSucceed,
        [switch]$Create
    )
    $caseRoot = Join-Path $fixtureRoot $Name
    $scripts = Join-Path $caseRoot 'scripts'
    $listing = Join-Path $caseRoot 'listing'
    [void](New-Item -ItemType Directory -Path $scripts, $listing)
    Copy-Item -LiteralPath $source -Destination (Join-Path $scripts 'release.ps1')
    if ($Create) {
        [void](New-Item -ItemType Directory -Path (Join-Path $caseRoot '.build'))
        Set-Content -LiteralPath (Join-Path $caseRoot '.build/snowflake.yml') -Value 'definition_version: 2'
    }
    Set-Content -LiteralPath (Join-Path $listing 'listing-manifest.template.yml') -Value 'targets: <CONSUMER_ACCOUNTS>'
    @{
        version = $(if ($Scenario.ContainsKey('configVersion')) { $Scenario.configVersion } else { 'v1' })
        snowConnection = 'offline-test'
        distribution = 'EXTERNAL'
        consumerTargets = @('ORG.ACCOUNT')
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $scripts 'native-app-config.json')
    $Parameters | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $caseRoot 'parameters.json')
    $Scenario | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $caseRoot 'scenario.json')
    @'
param([string]$Config)
@{ kind = 'build'; location = (Get-Location).Path } | ConvertTo-Json -Compress | Add-Content -LiteralPath $env:RELEASE_TEST_LOG
$global:LASTEXITCODE = 0
'@ | Set-Content -LiteralPath (Join-Path $scripts 'build-and-push.ps1')
    @'
$ErrorActionPreference = 'Stop'
$env:RELEASE_TEST_LOG = Join-Path $PSScriptRoot 'calls.jsonl'
$global:ReleaseScenario = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'scenario.json') -Raw | ConvertFrom-Json
$global:ReleaseShowCount = 0
$global:LASTEXITCODE = 0
function global:snow {
    $callArgs = @($args)
    @{ kind = 'snow'; arguments = $callArgs; location = (Get-Location).Path } | ConvertTo-Json -Depth 5 -Compress | Add-Content -LiteralPath $env:RELEASE_TEST_LOG
    $global:LASTEXITCODE = 0
    if ($callArgs[0] -eq 'sql') {
        $queryIndex = [array]::IndexOf($callArgs, '-q')
        if ($queryIndex -lt 0) { $queryIndex = [array]::IndexOf($callArgs, '--query') }
        $query = $callArgs[$queryIndex + 1]
        if ($query -match '^SHOW APPLICATION PACKAGES') {
            if ($global:ReleaseScenario.failPackageQuery) { $global:LASTEXITCODE = 7; return '[]' }
            if ($global:ReleaseScenario.malformedPackage) { return 'invalid JSON' }
            $distribution = if ($global:ReleaseScenario.distribution) { $global:ReleaseScenario.distribution } else { 'EXTERNAL' }
            return ConvertTo-Json -InputObject @(@{ name = 'MENDIX_SPCS_PKG'; distribution = $distribution }) -Compress
        }
        if ($query -match '^SHOW VERSIONS') {
            $global:ReleaseShowCount++
            if ($global:ReleaseScenario.failVersionsQuery) { $global:LASTEXITCODE = 8; return '[]' }
            if ($global:ReleaseScenario.malformedVersions) { return '{not JSON' }
            $rows = @()
            if ($global:ReleaseShowCount -gt 1 -and $null -ne $global:ReleaseScenario.versionsAfter) {
                $rows = @($global:ReleaseScenario.versionsAfter)
            } else { $rows = @($global:ReleaseScenario.versionsBefore) }
            $rowJson = ConvertTo-Json -InputObject $rows -Depth 8 -Compress
            if ($global:ReleaseScenario.nestedVersions) { return '[' + $rowJson + ']' }
            return $rowJson
        }
        if ($query -match '^ALTER APPLICATION PACKAGE') {
            if ($global:ReleaseScenario.failPromotion) { $global:LASTEXITCODE = 9 }
            return '[]'
        }
    }
    if (($callArgs[0..2] -join ' ') -eq 'app version create') {
        if ($global:ReleaseScenario.failCreate) { $global:LASTEXITCODE = 10 }
        return 'Mock version created'
    }
    throw ('Unexpected snow command: ' + ($callArgs -join ' '))
}
$parameters = @{}
(Get-Content -LiteralPath (Join-Path $PSScriptRoot 'parameters.json') -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $parameters[$_.Name] = $_.Value }
try {
    & (Join-Path $PSScriptRoot 'scripts/release.ps1') @parameters
    exit 0
} catch {
    [Console]::Error.WriteLine($_.ToString())
    exit 1
}
'@ | Set-Content -LiteralPath (Join-Path $caseRoot 'run.ps1')

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = (Join-Path $PSHOME 'powershell.exe')
    if (-not (Test-Path -LiteralPath $startInfo.FileName)) { $startInfo.FileName = (Get-Process -Id $PID).Path }
    $startInfo.Arguments = '-NoProfile -NonInteractive -File "' + (Join-Path $caseRoot 'run.ps1') + '"'
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(30000)) { $process.Kill(); throw "$Name exceeded 30 seconds." }
        $output = $stdout.Result + $stderr.Result
        Assert-True (($process.ExitCode -eq 0) -eq $ShouldSucceed) "$Name returned $($process.ExitCode). $output"
    } finally { $process.Dispose() }

    $callsPath = Join-Path $caseRoot 'calls.jsonl'
    $calls = if (Test-Path -LiteralPath $callsPath) { @(Get-Content -LiteralPath $callsPath | ForEach-Object { $_ | ConvertFrom-Json }) } else { @() }
    $mutations = @($calls | Where-Object { $_.kind -eq 'snow' -and ($_.arguments -join ' ') -match 'ALTER APPLICATION PACKAGE' })
    if ($Create) {
        Assert-True ($mutations.Count -eq 0) "$Name promoted a release during creation."
        $creates = @($calls | Where-Object { $_.kind -eq 'snow' -and ($_.arguments[0..2] -join ' ') -eq 'app version create' })
        if ($ShouldSucceed) {
            Assert-True ($creates.Count -eq 1) "$Name did not create exactly one patch."
            $builds = @($calls | Where-Object { $_.kind -eq 'build' })
            $expectedBuilds = if ($Parameters.SkipBuild) { 0 } else { 1 }
            Assert-True ($builds.Count -eq $expectedBuilds) "$Name ran the wrong number of builds."
            $manifestPath = Join-Path $listing 'listing-manifest.yml'
            Assert-True (Test-Path -LiteralPath $manifestPath) "$Name did not render the listing manifest."
            $manifest = Get-Content -LiteralPath $manifestPath -Raw
            Assert-True ($manifest.Trim() -eq 'targets: ["ORG.ACCOUNT"]') "$Name rendered incorrect consumer targets."
        }
        foreach ($call in $creates) {
            Assert-True ($call.location -eq (Join-Path $caseRoot '.build')) "$Name created a version outside .build."
        }
    } else {
        Assert-True (@($calls | Where-Object { $_.kind -eq 'build' -or ($_.kind -eq 'snow' -and $_.arguments[0] -ne 'sql') }).Count -eq 0) "$Name built or created during promotion."
        Assert-True (-not (Test-Path -LiteralPath (Join-Path $listing 'listing-manifest.yml'))) "$Name rendered a manifest during promotion."
        if ($ShouldSucceed) {
            Assert-True ($mutations.Count -eq 1) "$Name did not promote exactly once."
            $expected = 'ALTER APPLICATION PACKAGE MENDIX_SPCS_PKG MODIFY RELEASE CHANNEL DEFAULT SET DEFAULT RELEASE DIRECTIVE VERSION = v1 PATCH = ' + $Parameters.Patch
            Assert-True (($mutations[0].arguments -join ' ') -match [regex]::Escape($expected)) "$Name used the wrong release directive."
        } elseif (-not $Scenario.failPromotion) {
            Assert-True ($mutations.Count -eq 0) "$Name attempted promotion after a failed guard."
        }
    }
    $script:passed++
    Write-Host "PASS $Name"
    return @{ Calls = $calls; Output = $output }
}

try {
    $approved = New-VersionRow 32
    $promote = @{ PublishRelease = $true; Patch = 32 }
    $baseline = @{ versionsBefore = @($approved) }
    [void](Invoke-ReleaseCase 'approved-promotion' $promote $baseline $true)
    [void](Invoke-ReleaseCase 'nested-json-promotion' $promote @{ versionsBefore = @($approved); nestedVersions = $true } $true)
    [void](Invoke-ReleaseCase 'patch-zero' @{ PublishRelease = $true; Patch = 0 } @{ versionsBefore = @((New-VersionRow 0)) } $true)
    foreach ($case in @(
        @{ Name = 'missing-patch'; Parameters = @{ PublishRelease = $true }; Scenario = $baseline },
        @{ Name = 'negative-patch'; Parameters = @{ PublishRelease = $true; Patch = -1 }; Scenario = $baseline },
        @{ Name = 'patch-without-promotion'; Parameters = @{ Patch = 32 }; Scenario = $baseline },
        @{ Name = 'promotion-with-skip-build'; Parameters = @{ PublishRelease = $true; Patch = 32; SkipBuild = $true }; Scenario = $baseline },
        @{ Name = 'promotion-with-skip-git-check'; Parameters = @{ PublishRelease = $true; Patch = 32; SkipGitCheck = $true }; Scenario = $baseline },
        @{ Name = 'rejected-patch'; Parameters = $promote; Scenario = @{ versionsBefore = @((New-VersionRow 32 'REJECTED')) } },
        @{ Name = 'missing-version'; Parameters = $promote; Scenario = @{ versionsBefore = @() } },
        @{ Name = 'wrong-patch'; Parameters = $promote; Scenario = @{ versionsBefore = @((New-VersionRow 31)) } },
        @{ Name = 'internal-live-package'; Parameters = $promote; Scenario = @{ versionsBefore = @($approved); distribution = 'INTERNAL' } },
        @{ Name = 'wrong-channel'; Parameters = $promote; Scenario = @{ versionsBefore = @((New-VersionRow 32 'APPROVED' 'QA')) } },
        @{ Name = 'malformed-package-json'; Parameters = $promote; Scenario = @{ versionsBefore = @($approved); malformedPackage = $true } },
        @{ Name = 'malformed-versions-json'; Parameters = $promote; Scenario = @{ malformedVersions = $true } },
        @{ Name = 'package-query-failed'; Parameters = $promote; Scenario = @{ failPackageQuery = $true } },
        @{ Name = 'versions-query-failed'; Parameters = $promote; Scenario = @{ failVersionsQuery = $true } },
        @{ Name = 'promotion-failed'; Parameters = $promote; Scenario = @{ versionsBefore = @($approved); failPromotion = $true } },
        @{ Name = 'invalid-version-identifier'; Parameters = $promote; Scenario = @{ versionsBefore = @($approved); configVersion = 'v1; DROP DATABASE x' } },
        @{ Name = 'duplicate-patch'; Parameters = $promote; Scenario = @{ versionsBefore = @($approved, $approved) } }
    )) {
        [void](Invoke-ReleaseCase $case.Name $case.Parameters $case.Scenario $false)
    }
    $dropped = New-VersionRow 32
    $dropped.dropped_on = '2026-09-16 12:00:00'
    [void](Invoke-ReleaseCase 'dropped-patch' $promote @{ versionsBefore = @($dropped) } $false)
    $created = Invoke-ReleaseCase 'create-patch' @{ SkipBuild = $true; SkipGitCheck = $true } @{ versionsBefore = @($approved); versionsAfter = @($approved, (New-VersionRow 33 'PENDING')) } $true -Create
    Assert-True ($created.Output -match '(?i)patch\D+33') 'Creation did not report the new patch 33.'
    Assert-True (@($created.Calls | Where-Object { ($_.arguments -join ' ') -match 'app version create.*--skip-git-check' }).Count -eq 1) 'Creation lost --skip-git-check.'
    [void](Invoke-ReleaseCase 'create-with-build' @{} @{ versionsBefore = @(); versionsAfter = @((New-VersionRow 0 'PENDING')) } $true -Create)
    [void](Invoke-ReleaseCase 'create-failed' @{ SkipBuild = $true } @{ versionsBefore = @($approved); failCreate = $true } $false -Create)
    [void](Invoke-ReleaseCase 'create-ambiguous-patch' @{ SkipBuild = $true } @{ versionsBefore = @($approved); versionsAfter = @($approved, (New-VersionRow 33), (New-VersionRow 34)) } $false -Create)
    [void](Invoke-ReleaseCase 'create-no-new-patch' @{ SkipBuild = $true } @{ versionsBefore = @($approved); versionsAfter = @($approved) } $false -Create)
    Write-Host "$passed offline release checks passed."
} finally {
    $resolvedRoot = [System.IO.Path]::GetFullPath($fixtureRoot)
    $tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolvedRoot.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe fixture cleanup path: $resolvedRoot" }
    Remove-Item -LiteralPath $resolvedRoot -Recurse -Force
}
