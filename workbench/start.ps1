param([ValidateSet('Start', 'Status', 'Stop', 'Open')][string]$Action = 'Start')
$ErrorActionPreference = 'Stop'
$workbenchRoot = (Get-Item -LiteralPath $PSScriptRoot).FullName
$newsRoot = Split-Path -Parent $workbenchRoot
. (Join-Path $newsRoot 'runtime-env.ps1')
$workbenchPython = Join-Path $newsRoot '.venv\Scripts\python.exe'
$workbenchScript = Join-Path $workbenchRoot 'start.py'

if ($Action -ne 'Open') {
    $startArguments = @('-B', $workbenchScript)
    if ($Action -eq 'Stop') { $startArguments += '--stop' }
    if ($Action -eq 'Status') { $startArguments += '--status' }
    & $workbenchPython @startArguments
    exit $LASTEXITCODE
}

function Find-Chrome {
    $registrations = @(
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe'
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe'
    )
    $candidates = foreach ($registration in $registrations) {
        $item = Get-Item -LiteralPath $registration -ErrorAction SilentlyContinue
        if ($item) { $item.GetValue('') }
    }
    $candidates = @($candidates) + @(
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
    )
    foreach ($candidate in $candidates) {
        if ($candidate -is [string] -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $candidate
        }
    }
    throw '找不到 Google Chrome，请先安装 Chrome。'
}

function Get-WorkbenchUrl {
    $status = & $workbenchPython -B $workbenchScript --status
    if ($LASTEXITCODE) { throw '无法读取工作台状态。' }
    if ($status -is [string] -and $status -match '\Ahttp://127\.0\.0\.1:[0-9]{1,5}/\z') {
        $address = [uri]$status
        if ($address.Port -gt 0) { return $status }
    }
}

$chrome = Find-Chrome
$url = Get-WorkbenchUrl
if (-not $url) {
    $process = Start-Process -FilePath $workbenchPython -ArgumentList @('-B', ('"' + $workbenchScript + '"')) -WorkingDirectory $workbenchRoot -WindowStyle Hidden -PassThru
    $deadline = [datetime]::UtcNow.AddSeconds(30)
    while (-not $url -and [datetime]::UtcNow -lt $deadline) {
        if ($process.HasExited -and $process.ExitCode -ne 0) { break }
        Start-Sleep -Milliseconds 250
        $url = Get-WorkbenchUrl
    }
    if (-not $url) { throw "工作台未能启动。请查看 $workbenchRoot\runtime 中的日志。" }
}
Start-Process -FilePath $chrome -ArgumentList @($url)
Write-Output $url
