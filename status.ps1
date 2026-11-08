# 打印小鱼各服务的端口状态，等服务起来（最多 90 秒）再报。
# 给 启动.bat 用：真正的启动由 _launch.vbs 在无控制台环境里做，
# 这样关掉命令行窗口不会把服务一起带走。

$Root = $PSScriptRoot

function Test-Port([int]$Port) {
    # 2026-11-04（第二十轮）修：**不要用 GetActiveTcpListeners()**。
    #
    # 实测：本机上它只回报 12 个监听器里的 **1 个**（8898），
    # 5099/6185/6199/8848/3000 **全部看不到**，而
    # `Get-NetTCPConnection -LocalPort` 能全部看到。
    # 后果：status.ps1 等到 90 秒超时仍然报「有服务没起来」，
    # 而实际上六个服务都好好活着 —— 用户以为启动失败。
    #
    # 不用 netstat 同理：本机 netstat 对 python/node 服务也不显示 LISTENING 行
    # （第七轮在 stop.cmd 里踩过同一个坑）。
    #
    # Get-NetTCPConnection 是这台机器上唯一可靠的判据。
    # ⚠ 不按 State 过滤：某些监听器的 State 字段是空串（不是 "Listen"）。
    $c = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
         Where-Object { $_.OwningProcess -ne 0 } |
         Select-Object -First 1
    return ($null -ne $c)
}

$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
    if ((Test-Port 6185) -and (Test-Port 8898) -and (Test-Port 8848)) { break }
    Start-Sleep -Seconds 2
}

Write-Host ''
Write-Host '=== 端口 ===' -ForegroundColor Cyan
foreach ($p in 5099, 3000, 6185, 6199, 8848, 8898) {
    '{0,-6} {1}' -f $p, $(if (Test-Port $p) { 'LISTEN' } else { '-' })
}
Write-Host ''
Write-Host 'QQ WebUI  : http://localhost:6185'
Write-Host 'SnowLuma  : http://127.0.0.1:5099'

# 退出码决定 启动.bat 会不会把窗口留住。没有 exit 的话本脚本退出码恒为 0，
# 「失败就 pause」那条分支永远不成立，错误信息会跟着窗口一起消失。
if (-not ((Test-Port 6185) -and (Test-Port 8898) -and (Test-Port 8848))) {
    Write-Host ''
    Write-Host '有服务没起来，窗口保持打开；启动细节见 restart-start.log（restart 时）' -ForegroundColor Yellow
    exit 1
}
