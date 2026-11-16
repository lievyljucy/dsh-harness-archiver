#Requires -Version 5.1
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
    [switch]$NoCreateRepo
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Step([string]$msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Ok([string]$msg)   { Write-Host "  OK  $msg" -ForegroundColor Green }
function Die([string]$msg)  { Write-Host "  FAIL  $msg" -ForegroundColor Red; throw $msg }
function Info([string]$msg) { Write-Host "  ..  $msg" -ForegroundColor Gray }

$root = $PSScriptRoot
Set-Location $root

Step '0. Locate tools'
$git = (Get-Command git -ErrorAction SilentlyContinue).Source
$ghCmd = (Get-Command gh -ErrorAction SilentlyContinue)
if (-not $git) { Die 'git not on PATH. Open a NEW terminal (user PATH was updated).' }
Ok "git: $(& $git --version)"
if (-not $ghCmd) {
    Info 'gh not on current process PATH; using known install location'
    $ghExe = 'C:\Users\Administrator\AppData\Local\hermes\gh\gh.exe'
    if (-not (Test-Path -LiteralPath $ghExe)) { Die 'gh.exe missing' }
} else {
    $ghExe = $ghCmd.Source
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
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) {
    $py = 'C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'
}
if (Test-Path -LiteralPath $py) {
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
if ($SkipAuthCheck) {
    Info 'auth check skipped by flag'
} else {
    & $ghExe auth status 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host @'
  gh is not logged in. Run this once, then re-run push.ps1:

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
            & $ghExe repo create $RepoName $vis --confirm
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
if ($PSCmdlet.ShouldProcess($fullRepo, 'git push -u origin main')) {
    & $git push -u origin $branch
    if ($LASTEXITCODE -ne 0) { Die "git push failed (exit $LASTEXITCODE). Check auth / repo name." }
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
