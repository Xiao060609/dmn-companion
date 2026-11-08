# 小鱼 · 一键启动
# 1) QQ 那套（SnowLuma 5099 + AstrBot 6185）
# 2) 桌宠（8848，会自己拉起网关 8898）
# 3) DSH（DeepSeek Harness，路径在 desktop\config.json 的 ops.dsh_exe）
# 已在跑的部分会自动跳过，重复执行是安全的。

param(
    # 可选：把本次启动的全部输出写进这个文件。
    # restart.ps1 用隐藏窗口把本脚本拉起来（这样关掉重启窗口也不会中断拉起），
    # 那种情况下没有可见控制台，只能靠这个参数留日志。
    [string]$LogPath = ''
)

if ($LogPath) {
    # 写不了日志不算失败：启动本身比日志重要
    try { Start-Transcript -LiteralPath $LogPath -Force | Out-Null } catch { }
}

$ErrorActionPreference = 'Stop'

$Root     = $PSScriptRoot
$AppDir   = Join-Path $Root 'desktop\desktop'
$Electron = Join-Path $AppDir 'node_modules\electron\dist\electron.exe'
$QqScript = Join-Path $Root 'qq\启动-小鱼.ps1'
$QqLog    = Join-Path $Root 'qq\启动日志.log'

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

function Wait-Port([int]$Port, [int]$Seconds) {
    for ($i = 0; $i -lt $Seconds; $i++) {
        if (Test-Port $Port) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

Write-Host '=== 1/3 QQ 那套（SnowLuma + AstrBot）===' -ForegroundColor Cyan
if (-not (Test-Path -LiteralPath $QqScript)) {
    Write-Host "找不到 $QqScript" -ForegroundColor Red
} elseif ((Test-Port 6185) -and (Test-Port 6199)) {
    Write-Host '已经在跑，跳过。'
} else {
    # 交给独立进程跑：它的子进程会继承控制台句柄，
    # 在同一个进程里 & 调用会让本脚本一直等到子进程退出
    # -ArgumentList 是数组，PowerShell 用空格拼命令行、不补引号：路径含空格就必须
    # 自己带引号，否则 -File 后面会被拆成两段，脚本根本不跑（而且不报错）。
    Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $QqScript)) `
        -RedirectStandardOutput $QqLog -RedirectStandardError "$QqLog.err" `
        -WindowStyle Hidden
    if (Wait-Port 6185 90) { Write-Host '已启动。' } else { Write-Host "90s 没等到 6185，看 $QqLog" -ForegroundColor Red }
}

Write-Host ''
Write-Host '=== 2/3 桌宠（8848 / 网关 8898）===' -ForegroundColor Cyan
# 用 8848 端口判断，而不是 Get-Process electron：
# Electron 退出时子进程会残留，按进程名会误判「已经在跑」而跳过。
if (Test-Port 8848) {
    Write-Host '已经在跑，跳过。'
} elseif (-not (Test-Path -LiteralPath $Electron)) {
    Write-Host "找不到 Electron：$Electron" -ForegroundColor Red
} else {
    # 从某些 IDE / 终端继承来的这个变量会让 electron.exe 以纯 Node 身份跑，
    # 起来就退出、没有窗口
    Remove-Item Env:\ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
    Start-Process -FilePath $Electron -ArgumentList '.' -WorkingDirectory $AppDir | Out-Null
    if (Wait-Port 8898 30) { Write-Host '已启动。' } else { Write-Host '起来了但网关没就绪，看 desktop\desktop\gateway.log' -ForegroundColor Red }
}

Write-Host ''
Write-Host '=== 3/3 DSH（DeepSeek Harness）===' -ForegroundColor Cyan

# DSH 是独立的多进程 Electron 应用（不在本仓库里），路径登记在 desktop\config.json。
# 这一段任何时候都不许抛错中断：它是「顺带启动」，起不来也不能拖累 QQ 和桌宠。
$DshExe    = ''
$ManageDsh = $false
$DshCfg    = Join-Path $Root 'desktop\config.json'

# 必须按 ExecutablePath 精确匹配。按进程名（DeepSeek Harness）会命中别的安装位置的
# 同名程序，把用户另外一份 DSH 当成「已经在跑」，然后就再也不启动了。
function Get-DshProcesses([string]$Exe) {
    if (-not $Exe) { return @() }
    try {
        return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { try { $_.ExecutablePath -eq $Exe } catch { $false } })
    } catch {
        return @()
    }
}

try {
    # 这个 json 可能带 UTF-8 BOM（记事本存过），-Encoding UTF8 能正常吃掉
    $cfg = Get-Content -LiteralPath $DshCfg -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($cfg.ops) {
        $DshExe    = [string]$cfg.ops.dsh_exe
        # 没写 manage_dsh 就按 false 处理：宁可不管，也不去动用户自己的程序
        $ManageDsh = [bool]$cfg.ops.manage_dsh
    }
} catch {
    Write-Host "读不到 $DshCfg 的 ops 段，跳过 DSH。" -ForegroundColor Yellow
}

if (-not $ManageDsh) {
    Write-Host 'manage_dsh=false，不管 DSH。'
} elseif (-not $DshExe) {
    Write-Host '没配 ops.dsh_exe，跳过 DSH。'
} elseif (-not (Test-Path -LiteralPath $DshExe)) {
    Write-Host "找不到 DSH：$DshExe，跳过。" -ForegroundColor Yellow
} else {
    $dshRunning = Get-DshProcesses $DshExe
    if ($dshRunning.Count -gt 0) {
        Write-Host "已经在跑（$($dshRunning.Count) 个进程），跳过。"
    } else {
        try {
            # 不等待：DSH 起窗口要几秒，等它会把整个启动流程卡住
            Start-Process -FilePath $DshExe -WorkingDirectory (Split-Path -LiteralPath $DshExe)
            Write-Host '已启动。'
        } catch {
            Write-Host "启动 DSH 失败：$($_.Exception.Message)" -ForegroundColor Yellow
        }
    }
}

Write-Host ''
Write-Host '=== 端口 ===' -ForegroundColor Cyan
foreach ($p in 5099, 3000, 6185, 6199, 8848, 8898) {
    '{0,-6} {1}' -f $p, $(if (Test-Port $p) { 'LISTEN' } else { '-' })
}
if ($ManageDsh -and $DshExe -and (Test-Path -LiteralPath $DshExe)) {
    $dshNow = Get-DshProcesses $DshExe
    'DSH    {0}' -f $(if ($dshNow.Count -gt 0) { "RUNNING（$($dshNow.Count) 个进程）" } else { '未在跑' })
} else {
    'DSH    不管理（见 desktop\config.json 的 ops）'
}
Write-Host ''
Write-Host 'QQ WebUI  : http://localhost:6185'
Write-Host 'SnowLuma  : http://127.0.0.1:5099'

if ($LogPath) { try { Stop-Transcript | Out-Null } catch { } }
