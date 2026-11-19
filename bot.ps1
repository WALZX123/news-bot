param([Parameter(ValueFromRemainingArguments = $true)][string[]]$BotArguments)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'runtime-env.ps1')
$botPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
Push-Location -LiteralPath $PSScriptRoot
try {
    & $botPython -B main.py @BotArguments
    $botExit = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $botExit
