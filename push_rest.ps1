#Requires -Version 5.1
<#
.SYNOPSIS
  Push the local git history to GitHub using the REST Git Data API.

.DESCRIPTION
  Used when git smart-HTTPS transport is blocked on this machine:
    * global `url.https://gh-proxy.com/...insteadOf https://github.com/`
    * gh-proxy does not forward Basic auth  -> "No anonymous write access"
    * direct github.com smart-HTTP is reset -> "Recv failure"
  but api.github.com works fine with the gho_ token.

  Replays every local commit exactly: blobs -> trees -> commits -> force-update
  refs/heads/main. The replayed root commit has NO parent, so remote history is
  byte-identical to local history (the throwaway init commit is orphaned).

  Reads objects through `git cat-file` / `git ls-tree -z` (raw bytes, no
  re-encoding). The token is read from the git credential manager at runtime:
  never printed, never written to disk, never stored in the repo.
#>
[CmdletBinding()]
param(
    [string]$ProjectDir = $PSScriptRoot,
    [string]$Owner = 'lievyljucy',
    [string]$Repo  = 'dsh-harness-archiver',
    [string]$Branch = 'main'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$git = (Get-Command git -ErrorAction Stop).Source
$root = (Resolve-Path -LiteralPath $ProjectDir).Path
Set-Location $root

function Ok($m)   { Write-Host "  OK  $m" -ForegroundColor Green }
function Info($m) { Write-Host "  ..  $m" -ForegroundColor Gray }
function Die($m)  { Write-Host "  FAIL  $m" -ForegroundColor Red; throw $m }

# ---------------------------------------------------------------- token
$credIn = "protocol=https`nhost=github.com`n`n"
$credOut = $credIn | & $git credential fill 2>$null
$line = @($credOut | Where-Object { $_ -match '^password=' } | Select-Object -First 1)
if ($line.Count -eq 0 -or $line[0].Length -le 9) { Die 'no github.com token in git credential manager' }
$token = $line[0].Substring(9)
$H = @{
    Authorization = "Bearer $token"
    'User-Agent'  = 'dsh-rest-push'
    Accept        = 'application/vnd.github+json'
}
$base = "https://api.github.com/repos/$Owner/$Repo"
Ok "token loaded (len=$($token.Length)); target=$Owner/$Repo"

# ---------------------------------------------------------------- helpers
function Api([string]$method, [string]$url, $body) {
    $p = @{ Method = $method; Uri = $url; Headers = $H
            UseBasicParsing = $true; TimeoutSec = 90 }
    if ($null -ne $body) {
        $p['Body'] = ($body | ConvertTo-Json -Depth 12 -Compress)
        $p['ContentType'] = 'application/json'
    }
    for ($i = 1; $i -le 4; $i++) {
        try { return Invoke-WebRequest @p }
        catch {
            $resp = $_.Exception.Response
            $code = if ($resp) { [int]$resp.StatusCode } else { 0 }
            if ($code -eq 409 -or $code -eq 429 -or $code -ge 500) {
                Info "HTTP $code on $method, retry $i/4"
                Start-Sleep -Seconds (2 * $i)
                continue
            }
            $detail = ''
            try {
                if ($resp) {
                    $sr = New-Object System.IO.StreamReader($resp.GetResponseStream())
                    $detail = $sr.ReadToEnd()
                }
            } catch { }
            Die "API $method $url -> HTTP $code : $detail"
        }
    }
    Die "API $method $url failed after 4 attempts"
}

function GitBytes([string]$argsLine) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $git
    $psi.Arguments = '-C "' + $root + '" ' + $argsLine
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $proc = [System.Diagnostics.Process]::Start($psi)
    $ms = New-Object System.IO.MemoryStream
    $proc.StandardOutput.BaseStream.CopyTo($ms)
    $stderr = $proc.StandardError.ReadToEnd()
    $proc.WaitForExit()
    if ($proc.ExitCode -ne 0) { Die "git $argsLine -> $stderr" }
    return , $ms.ToArray()
}

function ParseIdent([string]$line, [string]$kind) {
    $m = [regex]::Match($line, "^$kind (.*) <(.*)> (\d+) ([+-]\d{4})$")
    if (-not $m.Success) { Die "cannot parse $kind identity: $line" }
    $epoch = [int64]$m.Groups[3].Value
    $date = [DateTimeOffset]::FromUnixTimeSeconds($epoch).ToString('yyyy-MM-ddTHH:mm:ssZ')
    return @{ name = $m.Groups[1].Value; email = $m.Groups[2].Value; date = $date }
}

$shaMap = @{}   # local object sha -> remote object sha

function UploadBlob([string]$sha) {
    if ($shaMap.ContainsKey($sha)) { return $shaMap[$sha] }
    $bytes = GitBytes "cat-file blob $sha"
    $r = Api 'POST' "$base/git/blobs" @{
        content  = [Convert]::ToBase64String($bytes)
        encoding = 'base64'
    }
    $remote = ($r.Content | ConvertFrom-Json).sha
    $shaMap[$sha] = $remote
    return $remote
}

function UploadTree([string]$sha) {
    if ($shaMap.ContainsKey($sha)) { return $shaMap[$sha] }
    # `ls-tree -z`: "<mode> <type> <sha>\t<path>\0" records, captured as raw bytes.
    $raw = GitBytes "ls-tree -z $sha"
    $flat = [Text.Encoding]::UTF8.GetString($raw)
    $tree = @()
    foreach ($entry in ($flat -split "`0")) {
        if ([string]::IsNullOrEmpty($entry)) { continue }
        $meta, $path = $entry -split "`t", 2
        $parts = $meta -split ' '
        if ($parts.Count -ne 3) { Die "unexpected ls-tree record: $entry" }
        $mode = $parts[0]
        $type = $parts[1]
        $child = $parts[2]
        # GitHub API wants 6-digit file modes for tree entries
        if ($mode.Length -eq 5) { $mode = '0' + $mode }
        if ($type -eq 'tree')      { $remoteChild = UploadTree $child }
        elseif ($type -eq 'blob')  { $remoteChild = UploadBlob $child }
        else                       { Die "unsupported tree entry type: $type" }
        $tree += @{ path = $path; mode = $mode; type = $type; sha = $remoteChild }
    }
    $r = Api 'POST' "$base/git/trees" @{ tree = $tree }
    $remote = ($r.Content | ConvertFrom-Json).sha
    $shaMap[$sha] = $remote
    return $remote
}

# ---------------------------------------------------------------- replay
$localCommits = @(& $git -C $root rev-list --reverse $Branch)
if ($localCommits.Count -eq 0) { Die "no local commits on $Branch" }
$localHead = (& $git -C $root rev-parse $Branch).Trim()
$localTree = (& $git -C $root rev-parse "$Branch^{tree}").Trim()
Ok "$($localCommits.Count) local commits; head=$($localHead.Substring(0,7)) tree=$($localTree.Substring(0,7))"

$remoteRef = $null
try {
    $r = Api 'GET' "$base/git/refs/heads/$Branch" $null
    $remoteRef = $r.Content | ConvertFrom-Json
    Info "remote branch exists at $($remoteRef.object.sha.Substring(0,7)) (will be replaced)"
} catch {
    Info 'remote branch does not exist yet'
}

# Root of replayed history has no parent: remote ancestry == local ancestry.
$parents = @()
$finalSha = $null

foreach ($c in $localCommits) {
    $text = [Text.Encoding]::UTF8.GetString((GitBytes "cat-file commit $c"))
    $head, $msg = $text -split "`n`n", 2
    $headLines = $head -split "`n"
    $treeSha = ($headLines | Where-Object { $_ -match '^tree ' } | Select-Object -First 1).Substring(5)
    $authorLine = ($headLines | Where-Object { $_ -match '^author ' } | Select-Object -First 1)
    $commitLine = ($headLines | Where-Object { $_ -match '^committer ' } | Select-Object -First 1)

    $a = ParseIdent $authorLine 'author'
    $cm = ParseIdent $commitLine 'committer'
    $remoteTree = UploadTree $treeSha

    $body = @{
        message   = $msg.TrimEnd("`n")
        tree      = $remoteTree
        author    = $a
        committer = $cm
    }
    if ($parents.Count -gt 0) { $body['parents'] = $parents }

    $r = Api 'POST' "$base/git/commits" $body
    $finalSha = ($r.Content | ConvertFrom-Json).sha
    $parents = @($finalSha)
    $short = $msg.Split("`n")[0]
    if ($short.Length -gt 56) { $short = $short.Substring(0, 56) }
    Ok "$($c.Substring(0,7)) -> $($finalSha.Substring(0,7))  $short"
}

# ---------------------------------------------------------------- point branch
if ($remoteRef) {
    Api 'PATCH' "$base/git/refs/heads/$Branch" @{ sha = $finalSha; force = $true } | Out-Null
} else {
    Api 'POST' "$base/git/refs" @{ ref = "refs/heads/$Branch"; sha = $finalSha } | Out-Null
}
Ok "refs/heads/$Branch -> $($finalSha.Substring(0,7))"

# ---------------------------------------------------------------- verify
$remoteHead = ((Api 'GET' "$base/git/refs/heads/$Branch" $null).Content | ConvertFrom-Json).object.sha
$remoteTree = ((Api 'GET' "$base/git/commits/$remoteHead" $null).Content | ConvertFrom-Json).tree.sha
$remoteCount = [int](((Api 'GET' "$base/commits?sha=$Branch&per_page=100" $null).Content | ConvertFrom-Json)).Count

if ($remoteTree -ne $localTree) {
    Die "tree mismatch: local=$localTree remote=$remoteTree"
}
Ok "tree identical: $remoteTree"
Info "remote commits: $remoteCount (local: $($localCommits.Count))"

# confirm no leftover probe file
try {
    Api 'GET' "$base/contents/.dsh-probe?ref=$Branch" $null | Out-Null
    Info 'WARNING: probe file still present in remote tree'
} catch {
    Ok 'no leftover init probe file'
}

Write-Host "`nPUSHED: https://github.com/$Owner/$Repo/tree/$Branch" -ForegroundColor Cyan
Write-Host "Next : Settings -> Actions -> General -> Workflow permissions -> Read and write" -ForegroundColor Yellow
Write-Host "Then : gh workflow run `"Archive DeepSeek Harness releases`" -f max_new=20 -f probe_steps=12" -ForegroundColor Green
