#Requires -Version 5.1
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 lievyljucy
#
# Original implementation written for this repository. Contains no third-party
# source fragments; only git, gh and built-in PowerShell features are used.
<#
.SYNOPSIS
  Push the verified DSH archiver repo to GitHub and print the first-run commands.

.DESCRIPTION
  Does NOT create a commit (one already exists locally). Verifies tools, clean
  tree, LF-only staged blobs, gh auth; then creates the remote repo (optional),
  sets origin, pushes, and prints the exact first-run checklist.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\push.ps1 -RepoName dsh-harness-archiver
.EXAMPLE
  .\push.ps1 -RepoOwner lievyljucy -RepoName dsh-harness-archiver -NoCreateRepo
.EXAMPLE
  .\push.ps1 -RepoName dsh-harness-archiver -WhatIf
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$RepoOwner = '',
    [string]$RepoName  = 'dsh-harness-archiver',
    [switch]$Private,
    [switch]$SkipAuthCheck,
    [switch]$NoCreateRepo,
    [string]$ProjectDir = ''  # repo directory; defaults to this script's folder
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Step([string]$msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Ok([string]$msg)   { Write-Host "  OK  $msg" -ForegroundColor Green }
function Die([string]$msg)  { Write-Host "  FAIL  $msg" -ForegroundColor Red; throw $msg }
function Info([string]$msg) { Write-Host "  ..  $msg" -ForegroundColor Gray }

function Resolve-Exe([string]$name) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { return $cmd.Source }
    return $null
}


$root = if ($ProjectDir) { (Resolve-Path -LiteralPath $ProjectDir).Path } else { $PSScriptRoot }
Set-Location $root
Ok "project dir: $root"

Step '0. Locate tools'
$git = Resolve-Exe git
if (-not $git) { Die 'git not found on PATH. Install git or add it to PATH.' }
Ok "git: $(& $git --version)"

# Resolve tools without hardcoding any absolute path: process PATH first, then the
# persisted (registry) PATH so a shell with a stale environment still works, then
# explicit $env:GH_EXE / $env:DSH_PYTHON overrides.
function Find-OnPersistedPath([string]$exe) {
    $roots = @(
        [Environment]::GetEnvironmentVariable('Path', 'Machine'),
        [Environment]::GetEnvironmentVariable('Path', 'User')
    ) | Where-Object { $_ }
    foreach ($root in $roots) {
        foreach ($dir in ($root -split ';')) {
            if (-not $dir) { continue }
            $candidate = Join-Path $dir.Trim() $exe
            if (Test-Path -LiteralPath $candidate) { return $candidate }
        }
    }
    return $null
}

$ghExe = Resolve-Exe gh
if (-not $ghExe -and $env:GH_EXE) { $ghExe = $env:GH_EXE }
if (-not $ghExe) { $ghExe = Find-OnPersistedPath 'gh.exe' }
if (-not $ghExe) {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\GitHubCLI\bin\gh.exe'),
        (Join-Path $env:ProgramFiles 'GitHub CLI\gh.exe')
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    if ($candidates) { $ghExe = @($candidates)[0] }
}
if (-not $ghExe) {
    Info 'gh not found. Set $env:GH_EXE to its full path, or open a NEW terminal.'
    Die 'gh not found'
}
Ok "gh: $(& $ghExe --version | Select-Object -First 1)"

Step '1. Repository state'
if (-not (Test-Path -LiteralPath '.git')) { Die 'Not a git repo. Run git init -b main first.' }
$branch = (& $git branch --show-current).Trim()
Ok "branch: $branch"
$dirty = @(& $git status --porcelain)
if ($dirty.Count -gt 0) {
    Write-Host ($dirty -join "`n") -ForegroundColor Yellow
    Die "Working tree has $($dirty.Count) uncommitted change(s). Commit them first."
}
Ok 'working tree clean'
$log = (& $git log -1 --oneline).Trim()
Ok "HEAD: $log"
$tracked = @(& $git ls-files)
$big = @($tracked | Where-Object { (Get-Item -LiteralPath $_).Length -gt 1MB })
if ($big.Count -gt 0) { Die "Large tracked files found: $($big -join ', ')" }
Ok "$($tracked.Count) tracked files, all < 1 MB (no binaries)"

Step '2. LF-only check on staged blobs (Linux runner safety)'
# Python is optional here: the check is skipped if no interpreter is reachable.
$py = Resolve-Exe python
if (-not $py) { $py = Resolve-Exe python3 }
if (-not $py -and $env:DSH_PYTHON) { $py = $env:DSH_PYTHON }
if ($py -and (Test-Path -LiteralPath $py)) {
    $check = @'
import subprocess, sys
bad = []
for f in subprocess.check_output(["git","ls-files"], text=True).splitlines():
    b = subprocess.check_output(["git","show",":"+f])
    if b"\r" in b: bad.append(f)
print("TRACKED", len(subprocess.check_output(["git","ls-files"],text=True).splitlines()))
print("CR_FILES", bad)
sys.exit(1 if bad else 0)
'@
    $check | & $py -
    if ($LASTEXITCODE -ne 0) { Die 'Some tracked files contain CR. Check .gitattributes.' }
    Ok 'no CR bytes in any tracked blob'
} else {
    Info 'python not found; skipping byte-level CR check (see VERIFICATION.md)'
}

Step '3. GitHub authentication'
# gh needs GH_TOKEN. If nothing is set, try the Windows git credential manager
# (this machine has a valid gho_ token for github.com there). Never print it.
if (-not $env:GH_TOKEN -and -not $SkipAuthCheck) {
    try {
        $credIn = "protocol=https`nhost=github.com`n`n"
        $credOut = $credIn | & $git credential fill 2>$null
        $line = @($credOut | Where-Object { $_ -match '^password=' } | Select-Object -First 1)
        if ($line.Count -gt 0 -and $line[0].Length -gt 9) {
            $env:GH_TOKEN = $line[0].Substring(9)
            Ok 'loaded GitHub token from git credential manager (not displayed)'
        }
    } catch { Info 'no credential-manager token available' }
}
if ($SkipAuthCheck) {
    Info 'auth check skipped by flag'
} else {
    & $ghExe auth status 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host @'
  gh is not logged in, and no usable credential-manager token was found.
  Run this once, then re-run push.ps1:

      gh auth login

  Choose: GitHub.com -> HTTPS -> Login with a web browser.
'@ -ForegroundColor Yellow
        Die 'gh auth login required'
    }
    Ok 'gh authenticated'
    if (-not $RepoOwner) {
        $RepoOwner = (& $ghExe api user --jq '.login').Trim()
    }
    Ok "owner: $RepoOwner"
}

if (-not $RepoOwner) { Die 'RepoOwner unknown (pass -RepoOwner when using -SkipAuthCheck)' }
$fullRepo = "$RepoOwner/$RepoName"
$remoteUrl = "https://github.com/$fullRepo.git"

Step "4. Remote: $fullRepo"
# `git remote get-url origin` writes to stderr when origin is absent, which
# PowerShell -File turns into a terminating ErrorRecord. `git remote` only lists
# names and never errors, so use that instead.
$remoteNames = @(& $git remote)
$existing = $null
if ($remoteNames -contains 'origin') {
    $existing = (& $git config --get remote.origin.url).Trim()
}
if ($existing) {
    Ok "origin already set: $existing"
} else {
    if (-not $NoCreateRepo) {
        if ($PSCmdlet.ShouldProcess($fullRepo, 'gh repo create')) {
            $vis = if ($Private) { '--private' } else { '--public' }
            & $ghExe repo create $RepoName $vis
            if ($LASTEXITCODE -ne 0) { Die "gh repo create failed (exit $LASTEXITCODE)" }
            Ok "created $fullRepo"
        }
    } else {
        Info 'CreateRepo=false; assuming repository already exists'
    }
    if ($PSCmdlet.ShouldProcess($remoteUrl, 'git remote add origin')) {
        & $git remote add origin $remoteUrl
        if ($LASTEXITCODE -ne 0) { Die 'git remote add failed' }
        Ok "origin -> $remoteUrl"
    }
}

Step '5. Push'
# If this machine rewrites github.com to a third-party proxy via a global
# `url.<base>.insteadOf` rule, `gh repo create` bakes the ALREADY-REWRITTEN URL
# into origin. Normalise origin back to the canonical URL before pushing.
# (If that rewrite's credential does not work, use push_rest.py instead (python push_rest.py --owner <account> --repo <repo>) — the
# REST path needs no git transport at all.)
$canonicalOrigin = "https://github.com/$fullRepo.git"
$cur = (& $git config --get remote.origin.url 2>$null | Select-Object -First 1)
if ($cur -and $cur -ne $canonicalOrigin) {
    if ($PSCmdlet.ShouldProcess($canonicalOrigin, 'git remote set-url origin')) {
        & $git remote set-url origin $canonicalOrigin
        if ($LASTEXITCODE -ne 0) { Die 'failed to normalise origin URL' }
        Ok "origin normalised to the canonical GitHub URL"
    }
}
if ($PSCmdlet.ShouldProcess($fullRepo, "git push -u origin $branch")) {
    $env:GIT_TERMINAL_PROMPT = '0'
    & $git push -u origin $branch
    if ($LASTEXITCODE -ne 0) { Die "git push failed (exit $LASTEXITCODE). Try -Direct, or check auth / repo name." }
    Ok "pushed $branch -> origin/$branch"
}
& $git remote -v

Step '6. Required one-time setting (must do on the website)'
Write-Host @"
  https://github.com/$fullRepo/settings/actions
  Settings -> Actions -> General -> Workflow permissions
      -> select "Read and write permissions" -> Save

  Without this the workflow cannot create Releases (403).
"@ -ForegroundColor Yellow

Step '7. First run (bootstrap all 9 verified versions)'
Write-Host @"
  gh workflow run "Archive DeepSeek Harness releases" -f max_new=20 -f probe_steps=12

  gh run list --workflow "Archive DeepSeek Harness releases" --limit 5
  gh run watch
  gh release list --limit 50
"@ -ForegroundColor Green

Step 'Done'
Ok "Repo: https://github.com/$fullRepo"
Ok "Details: PUSH.md | Evidence: VERIFICATION.md"
