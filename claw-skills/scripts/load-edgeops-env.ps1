# 从配置文件加载 EDGEOPS_ACCESS_TOKEN / EDGEOPS_BASE_URL 到当前 PowerShell 会话。
# 用法: . .\claw-skills\scripts\load-edgeops-env.ps1

function Import-EdgeOpsDotEnv {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    Get-Content -LiteralPath $Path | ForEach-Object {
        $line = ($_ -split '#', 2)[0].Trim()
        if (-not $line -or $line -notmatch '=') { return }
        $name, $value = $line -split '=', 2
        $name = $name.Trim()
        $value = $value.Trim().Trim('"').Trim("'")
        switch ($name) {
            'EDGEOPS_ACCESS_TOKEN' { $env:EDGEOPS_ACCESS_TOKEN = $value }
            'EDGEOPS_BASE_URL' { $env:EDGEOPS_BASE_URL = $value.TrimEnd('/') }
            'EDGEOPS_API_BASE_URL' { $env:EDGEOPS_API_BASE_URL = $value.TrimEnd('/') }
        }
    }
    return $true
}

function Import-EdgeOpsJson {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $cfg = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    $token = ''
    if ($cfg.PSObject.Properties['accessToken']) { $token = [string]$cfg.accessToken }
    elseif ($cfg.PSObject.Properties['access_token']) { $token = [string]$cfg.access_token }
    $token = $token.Trim()
    $base = ''
    if ($cfg.PSObject.Properties['baseUrl']) { $base = [string]$cfg.baseUrl }
    elseif ($cfg.PSObject.Properties['base_url']) { $base = [string]$cfg.base_url }
    $base = $base.Trim().TrimEnd('/')
    if ($token) { $env:EDGEOPS_ACCESS_TOKEN = $token }
    if ($base) { $env:EDGEOPS_BASE_URL = $base }
    return [bool]$token
}

$candidates = @()
if ($env:EDGEOPS_CONFIG) { $candidates += $env:EDGEOPS_CONFIG }
$candidates += @(
    "$HOME\.config\edgeops\config.json",
    "$HOME\.config\edgeops\edgeops.config.json",
    "$HOME\.hermes\edgeops.json",
    "$HOME\.hermes\edgeops.config.json"
)
if ($env:HERMES_HOME) {
    $candidates += @(
        "$($env:HERMES_HOME)\edgeops.json",
        "$($env:HERMES_HOME)\edgeops.config.json"
    )
}
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$candidates += @(
    (Join-Path $scriptRoot '..\edgeops.config.json'),
    (Join-Path $scriptRoot '..\edgeops.env'),
    (Join-Path (Get-Location) 'edgeops.config.json'),
    (Join-Path (Get-Location) 'edgeops.env'),
    "$HOME\.config\edgeops\.env"
)

$loaded = $false
foreach ($path in $candidates) {
    $resolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($path)
    if ($resolved -match '\.json$') {
        if (Import-EdgeOpsJson -Path $resolved) { $loaded = $true; break }
    } else {
        if (Import-EdgeOpsDotEnv -Path $resolved) {
            if ($env:EDGEOPS_ACCESS_TOKEN) { $loaded = $true; break }
        }
    }
}

if (-not $loaded -and -not $env:EDGEOPS_ACCESS_TOKEN) {
    Write-Error "Moso config not found. Copy edgeops.config.example.json to config.json and set accessToken, or set EDGEOPS_ACCESS_TOKEN."
}
