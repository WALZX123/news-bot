param(
    [ValidateSet('Start','Stop','Status')][string]$Action = 'Start',
    [ValidateSet('Full','Collect')][string]$Mode = 'Full'
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'runtime-env.ps1')
$automationPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$automationMain = Join-Path $PSScriptRoot 'main.py'

function Read-Automation {
    $result = & $automationPython -B $automationMain --scheduler-status
    if ($LASTEXITCODE) { throw 'Cannot read automation status.' }
    return ($result | ConvertFrom-Json)
}

if ($Action -eq 'Status') {
    Read-Automation | ConvertTo-Json -Depth 10
    exit 0
}
if ($Action -eq 'Stop') {
    $output = & $automationPython -B $automationMain --scheduler-stop
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    if (($output | ConvertFrom-Json).stop_requested) {
        Write-Output 'Stop requested. The current cycle may finish before the worker exits.'
    } else {
        Write-Output 'Automation is stopped.'
    }
    exit 0
}
$state = Read-Automation
$requestedMode = if ($Mode -eq 'Collect') { 'collect' } else { 'full' }
if ($state.active) {
    if ($state.mode -ne $requestedMode -or $state.requested_mode) {
        $output = & $automationPython -B $automationMain --scheduler-mode $requestedMode
        if ($LASTEXITCODE -or -not ($output | ConvertFrom-Json).mode_requested) {
            throw 'Unable to switch automation mode safely.'
        }
        $state = Read-Automation
    }
    $state | ConvertTo-Json -Depth 10
    exit 0
}
$oldInstance = $state.instance
$automationArgs = @('-B', ('"' + $automationMain + '"'))
if ($Mode -eq 'Collect') { $automationArgs += '--scheduler-collect' }
$startupLog = Join-Path $PSScriptRoot 'runtime\automation-start.log'
New-Item -ItemType Directory -Path (Split-Path -Parent $startupLog) -Force | Out-Null
$automationProcess = Start-Process -FilePath $automationPython -ArgumentList $automationArgs -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardError $startupLog -PassThru
$deadline = [datetime]::UtcNow.AddSeconds(20)
do {
    Start-Sleep -Milliseconds 250
    $state = Read-Automation
    if ($state.active -and $state.state -in @('waiting','running') -and $state.instance -and $state.instance -ne $oldInstance -and $state.mode -eq $requestedMode -and $state.heartbeat_at) {
        $state | ConvertTo-Json -Depth 10
        exit 0
    }
    if ($automationProcess.HasExited -and $automationProcess.ExitCode -ne 0) { break }
} while ([datetime]::UtcNow -lt $deadline)
throw "Automation failed to start. See $startupLog and news_bot.log."
