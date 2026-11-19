# 职责：为新闻机器人补齐本地工具路径和回环网络绕过。
# 已实现功能：复用 E 盘 Node 副本，追加 localhost 绕过并统一子进程的 UTF-8 设置。
# 只影响新闻机器人及其子进程。
$newsRoot = $PSScriptRoot
$runtimePaths = @('local-tools') | ForEach-Object {
    $candidate = Join-Path $newsRoot $_
    if (Test-Path -LiteralPath $candidate -PathType Container) { $candidate }
}
$env:PATH = ((@($runtimePaths) + @($env:PATH -split ';')) | Where-Object { $_ } | Select-Object -Unique) -join ';'
$env:NO_PROXY = ((@($env:NO_PROXY -split ',') + @('localhost', '127.0.0.1', '::1')) | Where-Object { $_ } | Select-Object -Unique) -join ','
$env:PYTHONUTF8 = '1'


# 保证被 Python 或其他工具捕获的 PowerShell 状态 JSON 也是 UTF-8。
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)