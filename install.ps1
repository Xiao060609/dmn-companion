# 小鱼 · 安装 / 体检脚本
# 用法：解压后双击，或在 PowerShell 里跑
#   powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
# 它只做检查和必要的安装，不代替 启动.bat。

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

Write-Host ''
Write-Host '=== 1. 前置检查 ===' -ForegroundColor Cyan

# 1a) QQ 客户端
$qq = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match '^qq$|^tim$' }
if ($qq) {
    Write-Host "  QQ 客户端：在跑（$($qq.Count) 个进程） ✅"
} else {
    Write-Host '  QQ 客户端：没检测到 ❌'
    Write-Host '  目标机器要装并登录 QQ（小鱼要注入它才能发话）。装好后重跑本脚本。'
}

# 1b) Python（gateway 和桌宠 server.py 需要；AstrBot 用包内 .venv，不依赖系统 Python）
$py = Get-Command python -ErrorAction SilentlyContinue
if ($py) {
    $ver = (python -V 2>&1 | Out-String).Trim()
    $major = [regex]::Match($ver, 'python (\d+)\.(\d+)', 'IgnoreCase')
    if ($major.Success -and [int]$major.Groups[1].Value -ge 3 -and [int]$major.Groups[2].Value -ge 10) {
        Write-Host "  Python：$ver ✅"
    } else {
        Write-Host "  Python 版本太低（$ver），需要 3.10+。装一下：https://www.python.org/downloads/" -ForegroundColor Yellow
    }
} else {
    Write-Host '  Python：没找到 ❌'
    Write-Host '  装 Python 3.10+（安装时勾 Add to PATH），然后重跑本脚本。' -ForegroundColor Yellow
}

# 1c) 端口冲突
$busy = @()
foreach ($p in 5099, 6185, 6199, 8848, 8898) {
    if (Test-Port $p) { $busy += $p }
}
if ($busy.Count -eq 0) {
    Write-Host '  端口 5099/6185/6199/8848/8898 全空 ✅'
} else {
    Write-Host "  端口 $($busy -join ', ') 已被占用 ⚠️（有同名服务在跑？）" -ForegroundColor Yellow
}

# 1d) API key
$sec = Join-Path $Root 'gateway\secrets.json'
$key = ''
if (Test-Path $sec) {
    try { $key = (Get-Content $sec -Raw | ConvertFrom-Json).dashscope_api_key } catch {}
}
if ($key) {
    $tail = $key.Substring([Math]::Max(0, $key.Length - 4))
    Write-Host "  API key：已填（...$tail） ✅"
} else {
    Write-Host '  API key：secrets.json 里是空的 ❌'
    Write-Host '  打开 gateway\secrets.json，把 dashscope_api_key 填上你自己的 key。' -ForegroundColor Yellow
}

# ---- 1e) 桌宠素材（2026-11-04 第二十四轮加）----
# 为什么必须有这一段：desktop\assets（5.8G Live2D 素材）和
# desktop\web\vendor（PixiJS + Live2D 运行时）都**刻意不进 git**
# （见 .gitignore）。所以 git clone 下来的仓库里没有它们。
#
# 缺了会怎样：桌宠窗口照样弹出来，但 canvas 里**什么都没有**（白屏），
# 而且没有任何报错 —— 用户只会觉得"这软件坏了"。
# 这和上面那段 Python 依赖是同一类问题：体检给虚假的安全感。
$assets = Join-Path $Root 'desktop\assets'
$vendor = Join-Path $Root 'desktop\web\vendor'
$missAssets = -not (Test-Path $assets)
$missVendor = -not (Test-Path $vendor)
if ($missAssets -or $missVendor) {
    Write-Host '  桌宠素材：缺东西 ❌' -ForegroundColor Yellow
    if ($missAssets) {
        Write-Host '    desktop\assets 不存在（Live2D 模型 / 语音模型都在这里，约 5.8G）' -ForegroundColor Yellow
    }
    if ($missVendor) {
        Write-Host '    desktop\web\vendor 不存在（PixiJS / Live2D 运行时）' -ForegroundColor Yellow
    }
    Write-Host '    这两个目录刻意不进 git（太大）。如果你是用 git clone 拿到的仓库，' -ForegroundColor Yellow
    Write-Host '    需要从原机器拷贝，或用完整压缩包解压。' -ForegroundColor Yellow
    Write-Host '    不补的话：桌宠窗口会出现，但里面是一片空白，且没有报错。' -ForegroundColor Yellow
} else {
    Write-Host '  桌宠素材：assets + vendor 都在 ✅'
}

# ---- 1.5 Python 依赖（2026-11-04 加）----
# 为什么必须有这一段：以前的体检只查 QQ/版本/端口/key 四项，**一项都不查 pip 包**，
# 于是一台新机器装好 Python 3.13 跑完本脚本会全绿通过，然后 启动.bat 起不来网关，
# 用户看到的是"起来了但网关没就绪，看 desktop\desktop\gateway.log"。
# 那是最难查的一类失败：体检给了虚假的安全感。
Write-Host ''
Write-Host '=== 1.5 Python 依赖 ===' -ForegroundColor Cyan

# 网关实际 import 的第三方包（漏一个都起不来）。greenlet 是 sqlalchemy async
# 的隐式依赖，报错信息里才提示，最容易漏 —— 见 gateway\memory\README.md。
$RequiredPy = @('numpy', 'faiss', 'jieba', 'sqlalchemy', 'sqlmodel', 'aiosqlite', 'greenlet')
$MissingPy = @()
foreach ($mod in $RequiredPy) {
    $probe = "import $mod" -replace '^import faiss$', 'import faiss'
    $out = & python -c $probe 2>&1
    if ($LASTEXITCODE -ne 0) { $MissingPy += $mod }
}
if ($MissingPy.Count -eq 0) {
    Write-Host "  Python 包（$($RequiredPy -join ' / ')）：齐 ✅"
} else {
    Write-Host "  缺 $($MissingPy.Count) 个包：$($MissingPy -join ', ') ❌" -ForegroundColor Yellow
    Write-Host '  装一下（用跑桌宠的那个 Python）：' -ForegroundColor Yellow
    Write-Host "    python -m pip install -r `"$Root\gateway\requirements.txt`"" -ForegroundColor Yellow
}

Write-Host ''
Write-Host '=== 2. 下一步 ===' -ForegroundColor Cyan
Write-Host '  1) 填好 secrets.json 的 key'
Write-Host '  2) 登录并打开 QQ 客户端（SnowLuma 要注入它）'
Write-Host '  3) 双击 启动.bat，等它打印出六行 LISTEN'
Write-Host '  4) 桌宠弹出来 + http://localhost:6185 能打开，就成了'
Write-Host ''
