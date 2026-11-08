# 小鱼 · 一键重启：先停干净，再全部拉起来
# 用法：双击 重启服务.bat（或在 cmd 里跑）
# 它先按 8848 → 8898 → 6185/6199 → 5099/3000 的顺序杀干净，
# 然后调用 start.ps1 重新拉起全套（已在跑的部分会自动跳过）。
# 唯一不碰的是语音引擎（breeze-server，3.5GB 模型）和 DSH：留着给新实例复用，重启才快。
# 要彻底释放显存 / 关掉 DSH，用桌宠的「退出」，那条路会优雅清理。

$ErrorActionPreference = 'Continue'
$Root = $PSScriptRoot

function Test-Port([int]$Port) {
    # 2026-11-04 修（第二十/二十一轮）：不要用 GetActiveTcpListeners()。
    #
    # 实测：本机上它只回报 12 个监听器里的 **1 个**（8898），
    # 5099/6185/6199/8848/3000 **全部看不到**；而
    # `Get-NetTCPConnection -LocalPort` 能全部看到。
    # 后果：脚本死等端口、最后报「有服务没起来」，而实际服务全活着。
    #
    # netstat 同样不行（第七轮在 stop.cmd 里踩过同一个坑）。
    # ⚠ 不按 State 过滤：某些监听器的 State 字段是空串（不是 "Listen"）。
    $c = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
         Where-Object { $_.OwningProcess -ne 0 } |
         Select-Object -First 1
    return ($null -ne $c)
}

# 双保险：除了按端口杀，还按进程名收残留。
# 端口探测的盲区：进程活着但端口已经死掉（比如今天的 SnowLuma 崩了，5099/3000/3001 全没监听），
# 只查端口抓不到 PID；按进程名扫能把这类孤儿进程也收干净。
# 注意：只杀命令行里含本小鱼项目路径的进程，绝不动其它 node/python/electron（IDE、别的项目）。
function Stop-ByProcessName([string[]]$Names, [string[]]$PathFilters, [string]$Label) {
    $killed = @()
    foreach ($n in $Names) {
        Get-CimInstance Win32_Process -Filter "Name='$n' OR Name='${n}.exe'" -ErrorAction SilentlyContinue | ForEach-Object {
            $cmd = $_.CommandLine
            if (-not $cmd) { return }
            $hit = $false
            foreach ($f in $PathFilters) { if ($cmd -match [regex]::Escape($f)) { $hit = $true; break } }
            if (-not $hit) { return }
            try {
                taskkill /PID $_.ProcessId /T /F 2>$null | Out-Null
                $killed += ($_.ProcessId.ToString())
            } catch {}
        }
    }
    if ($killed.Count -gt 0) {
        Write-Host "  $Label : 按命令行补杀 $($killed -join ', ')"
        Start-Sleep -Milliseconds 1500
    } else {
        Write-Host "  $Label : 没有属于本项目的残留进程"
    }
}

function Stop-ByPort([int[]]$Ports, [string]$Label) {
    $pids = @{}
    foreach ($p in $Ports) {
        # ⚠ 不要加 `-State Listen`（2026-11-04 第二十一轮修）。
        #
        # 本机有些监听器的 State 字段是**空串**（不是 "Listen"），
        # 带这个过滤会漏掉它们 —— 实测桌宠/网关的 8848、8898 就被漏掉，
        # 于是第一次「停」失效，要多"强杀一次"才停得掉。
        # 这和第七轮 stop.cmd 里踩的坑是同一个。
        #
        # 判据改成「有没有 OwningProcess」，不靠 State 字符串。
        $c = Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue |
             Where-Object { $_.OwningProcess -ne 0 }
        foreach ($i in $c | Select-Object -ExpandProperty OwningProcess -Unique) {
            if ($i -and $i -ne 0) { $pids[$i] = $true }
        }
    }
    if ($pids.Count -eq 0) {
        Write-Host "  $Label : 没在跑"
        return
    }
    # 先软停（SIGTERM 语义），给 5 秒，还在就强杀
    # 注意：变量名不能用 $pid（PowerShell 内置只读自动变量），用 $procId
    foreach ($procId in $pids.Keys) {
        try { taskkill /PID $procId /T /F 2>$null | Out-Null } catch {}
    }
    $left = 0
    for ($i = 0; $i -lt 10; $i++) {
        $still = @()
        foreach ($p in $Ports) {
            if (Test-Port $p) { $still += $p }
        }
        if ($still.Count -eq 0) { break }
        Start-Sleep -Milliseconds 500
    }
    if ($still.Count -eq 0) {
        Write-Host "  $Label : 已停（pid: $($pids.Keys -join ', ')）"
    } else {
        Write-Host "  $Label : 端口 $($still -join ', ') 还占着 ⚠️ 强杀一次" -ForegroundColor Yellow
        foreach ($p in $still) {
            # 同样不加 -State Listen（见上面 Stop-ByPort 开头的说明）
            $c = Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue |
                 Where-Object { $_.OwningProcess -ne 0 }
            foreach ($i in $c | Select-Object -ExpandProperty OwningProcess -Unique) {
                if ($i -and $i -ne 0) { taskkill /PID $i /T /F 2>$null | Out-Null }
            }
        }
        Start-Sleep -Seconds 2
    }
}

Write-Host ''
Write-Host '=== 1/2 停 ===' -ForegroundColor Cyan
# 按链路顺序停：先桌宠（会带走网关），再 QQ 那套
Stop-ByPort @(8848, 8898) '桌宠+网关'
Stop-ByPort @(5099, 3000, 6185, 6199) 'SnowLuma+AstrBot'

# 双保险：按端口可能漏掉"端口已死但进程还活着"的孤儿（比如崩了半截的 node / python）。
# 按命令行补杀，只杀命令行里含小鱼项目路径的进程，绝不误伤 IDE / 别的项目。
$Xiaoyu = $PSScriptRoot  # <repo>
# 语音引擎（breeze-server）**故意不重启**：它常驻 3.5GB 模型，杀掉重载要 10~30s。
# 重启后新起的 server.py 会通过 tts_breeze.start() 的 adopted 分支直接复用它。
# 真要释放那 3.5GB 显存，走「退出」（桌宠会调 /api/shutdown 优雅停掉引擎）。
Write-Host '  语音引擎 : 保持运行，不重启（新实例会复用它）'
Stop-ByProcessName @('electron') @("\小鱼\desktop", "xiaoyu\desktop") '桌宠 Electron'
Stop-ByProcessName @('node') @("小鱼\qq\SnowLuma", "xiaoyu\qq\SnowLuma") 'SnowLuma node'
# 过滤条件必须是「本项目的路径片段」，不能是解释器名字。
# 曾经这里还有一条按解释器版本匹配的过滤项：那会杀掉本机所有用 Python 3.13
# 的进程（IDE 的、别的项目的），跟第 19 行的承诺正好相反。已删除。
Stop-ByProcessName @('python') @("小鱼\gateway", "小鱼\desktop", "xiaoyu\gateway", "xiaoyu\desktop") '网关/桌宠 python'
# ⚠ 2026-11-04 修：`"uv\\python"` 里 PowerShell **不把 `\\` 当转义**，
# 两个反斜杠原样保留，于是 `[regex]::Escape()` 之后变成 `uv\\\\python`
# （= uv + 两个字面反斜杠 + python），永远匹配不上真实的 uv 宿主路径
# `...\uv\python\cpython-3.12.11...\python.exe`。实测：双反斜杠 False、单反斜杠 True。
# 而正在跑的 AstrBot 恰好就是 uv 宿主 —— 也就是说「进程活着但端口已死」的
# 孤儿保险对这一型一直没生效。
Stop-ByProcessName @('python') @("小鱼\qq\astrbot", "xiaoyu\qq\astrbot", "uv\python") 'AstrBot python'

Write-Host ''
# 等端口真正释放：taskkill 是异步的，杀完立刻起会让 start.ps1 的
# Test-Port 仍看到旧监听，误判「已经在跑」而跳过，结果 QQ 那套起不来。
$waitDeadline = (Get-Date).AddSeconds(20)
while ((Get-Date) -lt $waitDeadline) {
    $portsFree = -not (Test-Port 8848) -and -not (Test-Port 8898) -and
                 -not (Test-Port 6185) -and -not (Test-Port 6199) -and
                 -not (Test-Port 5099)
    # 端口释放还不够：Electron 的子进程会残留一会儿，start.ps1 若按进程名
    # 判断会误判「已经在跑」而跳过，重启后桌宠就再也不启动。
    $noElectron = -not (Get-Process electron -ErrorAction SilentlyContinue)
    if ($portsFree -and $noElectron) { break }
    Start-Sleep -Milliseconds 300
}

Write-Host '=== 2/2 起 ===' -ForegroundColor Cyan
# 清 Electron 环境怪味（从 IDE/终端继承来的变量会让 electron 秒退）
Remove-Item Env:\ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue

# 拉起交给一个隐藏的独立 powershell，而不是在这里用 & 调 start.ps1。
# 原因：本脚本跑在可见控制台里，而「停」已经做完了。如果用户在「起」还没跑完时
# 关掉窗口，CTRL_CLOSE 会把本脚本连同正在跑的 start.ps1 一起打死，服务就停在
# 半路 —— 看着就是「关了窗口服务就没了」。
# -WindowStyle Hidden 会让子进程拿到**自己的**控制台（不是继承本窗口的），
# 所以本窗口关不关都不影响它。这里故意不加 -RedirectStandardOutput：
# 它会让 Start-Process 走 UseShellExecute=false，在环境里有 HTTPS_PROXY /
# https_proxy 大小写重复时直接抛错；日志改用 start.ps1 自己的 -LogPath。
$StartPs1 = Join-Path $Root 'start.ps1'
$StartLog = Join-Path $Root 'restart-start.log'
# 值必须自己带引号：-ArgumentList 是数组，PowerShell 用空格拼成一条命令行、
# 不会补引号。项目现在在 <repo>（无空格）所以没事，但一旦挪到含空格的目录，
# 会变成 -File G:\My Files\...\start.ps1，start.ps1 根本不跑，
# 本脚本白等 90 秒打一张全「-」的表，而且没有任何报错。
Start-Process -FilePath 'powershell.exe' `
    -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $StartPs1), '-LogPath', ('"{0}"' -f $StartLog)) `
    -WindowStyle Hidden
Write-Host "  已在后台拉起（日志：$StartLog），等端口就绪（最多 90 秒）…"

# 等端口真的起来再报，否则打印的是一张过时的表
$upDeadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $upDeadline) {
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
Write-Host '桌宠      : http://localhost:8848'
Write-Host '网关      : http://127.0.0.1:8898'

# 退出码决定 restart.bat 会不会把窗口留住。没有 exit 的话本脚本退出码恒为 0，
# 「失败就 pause」那条分支永远不成立，错误信息会跟着窗口一起消失。
if (-not ((Test-Port 6185) -and (Test-Port 8898) -and (Test-Port 8848))) {
    Write-Host ''
    Write-Host "有服务没起来，看 $StartLog" -ForegroundColor Yellow
    exit 1
}
