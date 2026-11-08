# 小鱼 QQ 机器人 · 当前方案一键启动
# 顺序：SnowLuma(5099/3000/3001) → AstrBot(6185/6199)
# 两个都是脱离窗口的后台进程；已在跑的服务自动跳过，可重复执行。
# 旧方案（DSH + qq-bridge）已归档到 _归档\，本脚本不启动它。

$ErrorActionPreference = 'Stop'

$Root     = $PSScriptRoot
$SnowLuma = Join-Path $Root 'SnowLuma'
$AstrBot  = Join-Path $Root 'astrbot'
$Node     = Join-Path $SnowLuma 'node\node.exe'
$Py       = Join-Path $AstrBot '.venv\Scripts\python.exe'

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

function Wait-Port([int]$Port, [int]$Seconds) {
    for ($i = 0; $i -lt $Seconds; $i++) {
        if (Test-Port $Port) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

# Start-Process 的重定向会清空已有日志；先留一份 .bak，免得把上次的排错现场弄丢
#
# ⚠ 2026-11-04（第二十轮）修：这个函数以前会**把整个启动流程打断**。
#
# 现象：双击 启动.bat，SnowLuma 起来了（5099 LISTEN），但 AstrBot 永远起不来，
# 90 秒后 启动.bat 报「有服务没起来」，用户完全不知道发生了什么。
#
# 根因：
#   ① `$ErrorActionPreference = 'Stop'`（脚本第 6 行）—— 任何错误都中断脚本；
#   ② 日志文件正被**上一个还在跑的进程**写着（比如 SnowLuma 自己），
#      `Move-Item -Force` 在 Windows 上移不动被占用的文件 → 抛 IOException；
#   ③ 这里没有 try/catch → 异常冒出去 → 整个脚本停止 →
#      后面的 AstrBot 启动代码**一行都没执行**。
#
# 实测证据：`qq\启动日志.log.err` 里就是
#     Move-Item : 另一个程序正在使用此文件，进程无法访问
#         + Move-Item -LiteralPath $Path -Destination ($Path + '.bak') -Force
# 而 `snowluma.out.log` 的修改时间正好是启动那一刻（说明它正被写）。
#
# 修法：**轮转日志是"锦上添花"，失败了不该拦住启动**。
#   - 整个函数包 try/catch，失败就算了（新日志会覆盖旧的，只是丢了上次现场）
#   - 加 `-ErrorAction SilentlyContinue` 双保险
#   - 失败时打一行黄字说明，让人知道"上次日志没保住"而不是默默咽掉
function Rotate-Log([string]$Path) {
    try {
        if (-not (Test-Path -LiteralPath $Path)) { return }
        if ((Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue).Length -le 0) { return }
        # 目标 .bak 已存在也可能被占用：先试删（失败也无所谓）
        $bak = $Path + '.bak'
        if (Test-Path -LiteralPath $bak) {
            Remove-Item -LiteralPath $bak -Force -ErrorAction SilentlyContinue
        }
        Move-Item -LiteralPath $Path -Destination $bak -Force -ErrorAction Stop
    } catch {
        Write-Host "  （日志轮转跳过：$([System.IO.Path]::GetFileName($Path)) 正被占用，不影响启动）" -ForegroundColor DarkGray
    }
}

function Test-QQClient() {
    # SnowLuma 的 native 枚举的是「能跑起来的 QQ 主进程」。QQ 是多进程结构（渲染/子进程），
    # 不一定每个进程都有主窗口，所以只看「有 qq.exe 进程在跑」就够了，别拿主窗口当判据。
    $procs = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match '^qq$|^tim$' }
    if (-not $procs) { return $false }
    return ($procs | Where-Object { $_.Path }).Count -gt 0
}

Write-Host '=== 小鱼 QQ 机器人 · 启动 ===' -ForegroundColor Cyan
Write-Host ''

# ---- 守卫：旧方案的 qq-bridge 若在跑，会和 AstrBot 抢同一条消息 ----
if (Test-Port 3100) {
    Write-Host '⚠️  3100 被占用：旧方案 qq-bridge 正在运行。' -ForegroundColor Yellow
    Write-Host '    它和 AstrBot 会各回一次同一条 QQ 消息，请先关掉它。'
    Write-Host '    查进程：Get-CimInstance Win32_Process -Filter "Name=''node.exe''" | Select ProcessId,CommandLine'
    exit 1
}

# ---- 1/2 SnowLuma ----
Write-Host '--- 1/2 SnowLuma (5099) ---'
if (-not (Test-QQClient)) {
    Write-Host '  ⚠️  没检测到 QQ 客户端窗口（进程名 qq.exe）。' -ForegroundColor Yellow
    Write-Host '      SnowLuma 靠 PipeWatcher 轮询自动发现并注入 QQ 进程，但没有 QQ 挂着时没得注入。'
    Write-Host '      请先打开并登录小鱼那个 QQ 号（10000002），再重新跑本脚本。'
    exit 1
}

if (Test-Port 5099) {
    Write-Host '  已在运行，跳过（QQ 注入状态保持不变）'
} else {
    if (-not (Test-Path -LiteralPath $Node)) { throw "缺便携 Node：$Node" }
    # SnowLuma 2.2.0 要求 Node ^24.22.1 || >=26.12.1，系统 Node 25 会被版本闸门拒
    $env:SNOWLUMA_ACCEPT_EULA = '1'
    $env:SNOWLUMA_ACCEPT_PRIVACY = '1'
    Rotate-Log (Join-Path $SnowLuma 'snowluma.out.log')
    Rotate-Log (Join-Path $SnowLuma 'snowluma.err.log')
    Start-Process -FilePath $Node -ArgumentList 'index.mjs' `
        -WorkingDirectory $SnowLuma `
        -RedirectStandardOutput (Join-Path $SnowLuma 'snowluma.out.log') `
        -RedirectStandardError  (Join-Path $SnowLuma 'snowluma.err.log') `
        -WindowStyle Hidden
    if (Wait-Port 5099 60) {
        Write-Host '  已启动'
    } else {
        Write-Host '  ⚠️ 60s 内没起来，看 SnowLuma\snowluma.err.log' -ForegroundColor Red
        exit 1
    }
}

# ---- 2/2 AstrBot ----
Write-Host '--- 2/2 AstrBot (6185) ---'
if (Test-Port 6185) {
    Write-Host '  已在运行，跳过'
} else {
    if (-not (Test-Path -LiteralPath $Py)) { throw "缺 AstrBot 虚拟环境：$Py" }
    # 中文 Windows 控制台是 GBK，启动横幅里的 emoji 会触发 UnicodeEncodeError
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUTF8 = '1'
    Rotate-Log (Join-Path $AstrBot 'astrbot.out.log')
    Rotate-Log (Join-Path $AstrBot 'astrbot.err.log')
    Start-Process -FilePath $Py -ArgumentList '-u', 'main.py' `
        -WorkingDirectory $AstrBot `
        -RedirectStandardOutput (Join-Path $AstrBot 'astrbot.out.log') `
        -RedirectStandardError  (Join-Path $AstrBot 'astrbot.err.log') `
        -WindowStyle Hidden
    if (Wait-Port 6185 120) {
        Write-Host '  已启动'
    } else {
        Write-Host '  ⚠️ 120s 内没起来，看 astrbot\astrbot.err.log' -ForegroundColor Red
        exit 1
    }
}

# ---- 状态 ----
Write-Host ''
Write-Host '=== 状态 ==='
foreach ($p in 5099, 3000, 3001, 6185, 6199, 8848) {
    '{0,-6} {1}' -f $p, $(if (Test-Port $p) { 'LISTEN' } else { '-' })
}

# ---- 连接校验（带重试）----
$linked = $false
# SnowLuma 的 PipeWatcher 会轮询发现并自动注入 QQ 进程（每 5s 扫一次），
# 注入 + AstrBot 侧 wsClient 重连需要几秒，先给 30s 缓冲再判，避免误报"未连接"
for ($i = 0; $i -lt 30; $i++) {
    try {
        $linked = [bool](Get-NetTCPConnection -State Established -LocalPort 6199 -ErrorAction Stop)
        if ($linked) { break }
    } catch { $linked = $false }
    Start-Sleep -Seconds 1
}

Write-Host ''
if ($linked) {
    Write-Host 'AstrBot ⇄ SnowLuma：已连接 ✅'
} else {
    Write-Host 'AstrBot ⇄ SnowLuma：30s 内没连上 ⚠️' -ForegroundColor Red
    Write-Host '  可能原因：QQ 没登录 / SnowLuma 没注入到 QQ 进程。'
    Write-Host '  去 http://127.0.0.1:5099 →「进程注入」手动加载一次（账号 admin）。'
}

Write-Host ''
Write-Host 'WebUI：AstrBot http://localhost:6185    SnowLuma http://127.0.0.1:5099'
Write-Host '8848 是语音用的 TTS，没开只影响语音，不影响文字。'
