"""脚本文件的编码规则（2026-11-04 第十九轮）。

为什么单独一个文件测这个
------------------------
这类问题**单测永远抓不到**（它不涉及任何 Python 函数），
但后果很严重：**新用户双击安装直接失败**。

2026-11-04 那天的实际事故：

    install.ps1 和 restart.ps1 是 **UTF-8 无 BOM**，而它们含中文。
    PowerShell 5.1 读无 BOM 的 UTF-8 文件会按 ANSI(GBK) 解释 ——
    中文变成乱码 → 引号配对错乱 → 报 `Unexpected token '}'`。
    实测 install.ps1 有 **4 处语法错误**，双击安装 100% 失败。

    而这两个文件正是我当天改过的（改名 / 加依赖检查段），
    改的时候用 Python 写盘，把原文件的 BOM 弄丢了。

本项目的编码规则（写在这里当代码，不只是注释）
----------------------------------------------
| 扩展名 | 编码 | 为什么 |
|---|---|---|
| `.ps1` | **UTF-8 with BOM** | PowerShell 5.1 无 BOM 时按 ANSI 读，中文必崩 |
| `.bat` / `.cmd` | **纯 ASCII** | cmd 按 ANSI 码页读，中文会变乱码路径 |
| `.md` / `.py` | UTF-8 无 BOM | 通用约定；带 BOM 反而让一些工具报错 |

这几条都是踩过坑才定下来的，所以用测试钉住，不靠人记。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: 会被用户直接执行的 .ps1（不在子目录深处，也不需要遍历 node_modules）
PS1_GLOBS = ("*.ps1",)


def _iter_files(pattern: str) -> list[Path]:
    """列出项目根下的匹配文件（不递归，避开 node_modules / .venv）。"""
    return sorted(p for p in ROOT.glob(pattern) if p.is_file())


def _has_chinese(data: bytes) -> bool:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


@pytest.mark.parametrize("path", _iter_files("*.ps1"), ids=lambda p: p.name)
def test_含中文的ps1必须有BOM(path: Path):
    """`.ps1` 含中文就必须带 UTF-8 BOM，否则 PowerShell 5.1 会解析失败。

    实测后果：install.ps1 曾因此报 4 处语法错误，双击安装直接失败。
    """
    raw = path.read_bytes()
    if not _has_chinese(raw):
        pytest.skip(f"{path.name} 不含中文，不需要 BOM")
    assert raw.startswith(b"\xef\xbb\xbf"), (
        f"{path.name} 含中文但**没有 UTF-8 BOM** ——\n"
        f"PowerShell 5.1 会按 GBK 解释，中文乱码导致语法错误，"
        f"用户双击就会失败。\n"
        f"修法：用 'utf-8-sig' 写盘，或前面补 b'\\xef\\xbb\\xbf'。"
    )


@pytest.mark.parametrize("path", _iter_files("*.bat") + _iter_files("*.cmd"),
                         ids=lambda p: p.name)
def test_bat和cmd必须纯ASCII(path: Path):
    """`.bat` / `.cmd` 必须纯 ASCII。

    cmd 按 ANSI 码页（中文系统是 GBK）读批处理文件，
    UTF-8 中文会变成乱码，**连注释都会被当命令执行**。
    """
    raw = path.read_bytes()
    try:
        raw.decode("ascii")
    except UnicodeDecodeError as exc:
        pytest.fail(
            f"{path.name} 含非 ASCII 字节（偏移 {exc.start}）——\n"
            f"cmd 按 ANSI 码页读批处理，中文会变乱码路径，"
            f"甚至把注释当命令执行。\n"
            f"修法：中文放到配套的 .ps1 里，.bat 只保留 ASCII。"
        )


def test_install_ps1_引用的文件名真实存在():
    """`install.ps1` 的用法注释里写的文件名必须真实存在。

    踩过的坑：第 5 轮把 `安装.ps1` 改名成 `install.ps1` 后，
    文件内的用法注释和 `安装说明.md` 还写着旧名 —— 文档指向不存在的文件。
    """
    import re

    path = ROOT / "install.ps1"
    if not path.exists():
        pytest.skip("install.ps1 不存在")
    src = path.read_text(encoding="utf-8-sig")
    # 找注释里提到的 .ps1 文件名
    mentioned = set(re.findall(r"([\w\-\u4e00-\u9fff]+\.ps1)", src))
    for name in mentioned:
        assert (ROOT / name).exists(), (
            f"install.ps1 的注释里提到 {name}，但项目根下没有这个文件 "
            f"（改名后忘了更新注释？）"
        )


# --------------------------------------------------------------------------- #
# 启动脚本的两个真实 bug（2026-11-04 第二十轮）
# --------------------------------------------------------------------------- #
def _ps1(name: str) -> str:
    from pathlib import Path
    p = Path(__file__).resolve().parents[2] / name
    if not p.exists():
        pytest.skip(f"{name} 不存在")
    return p.read_text(encoding="utf-8-sig")


@pytest.mark.parametrize("name", ["status.ps1", "start.ps1"])
def test_端口检测不能用_GetActiveTcpListeners(name: str):
    """`Test-Port` 必须用 `Get-NetTCPConnection`，不能用 `GetActiveTcpListeners()`。

    实测（2026-11-04）：本机上 `GetActiveTcpListeners()` 只回报了
    **12 个监听器里的 1 个**（8898），5099/6185/6199/8848/3000 **全部看不到**，
    而 `Get-NetTCPConnection -LocalPort` 能全部看到。

    后果：`status.ps1` 等到 90 秒超时仍然报「有服务没起来」，而实际上
    六个服务都好好活着 —— **用户以为启动失败**。
    实测修复前后：耗时 91.8s/退出码 1 → 55.6s/退出码 0。

    （`netstat` 也不行 —— 第七轮在 stop.cmd 里踩过同一个坑。）
    """
    src = _ps1(name)
    # ⚠ 只看**代码行**，不看注释 —— 我修这个 bug 时在注释里写了
    # "不要用 GetActiveTcpListeners()"，第一版测试就匹配到那句注释，
    # 误判成"还在用"。凡是要判"代码里有没有某东西"，都得先去掉注释。
    code_lines = [
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)
    assert "GetActiveTcpListeners" not in code, (
        f"{name} 的**代码**里还在用 GetActiveTcpListeners() —— 它在本机看不到"
        f"大部分端口，会把正常启动报成失败。请用 Get-NetTCPConnection -LocalPort。"
    )
    assert "Get-NetTCPConnection" in code, f"{name} 应该用 Get-NetTCPConnection 检测端口"


def test_日志轮转失败不该打断启动():
    """`Rotate-Log` 必须包 try/catch。

    踩过的坑（2026-11-04 第二十轮）：
      `qq\\启动-小鱼.ps1` 里有 `$ErrorActionPreference = 'Stop'`，
      而 `Rotate-Log` 用 `Move-Item -Force` 轮转日志。
      当日志正被上一个还在跑的进程写着时（比如 SnowLuma 自己），
      Windows 移不动 → 抛 IOException → **整个启动脚本中断** →
      后面的 AstrBot 启动代码一行都没执行 → 6185 永远起不来。

    修法：轮转日志是"锦上添花"，失败不该拦住启动。
    """
    src = _ps1("qq/启动-小鱼.ps1")
    # 找到 Rotate-Log 函数体
    import re as _re
    m = _re.search(r"function Rotate-Log.*?\n\}", src, _re.S)
    assert m, "找不到 Rotate-Log 函数"
    body = m.group(0)
    assert "try" in body and "catch" in body, (
        "Rotate-Log 没有 try/catch —— 日志被占用时会把整个启动流程打断。"
        "这是实测过的：AstrBot 因此永远起不来。"
    )


def test_所有脚本都不用_GetActiveTcpListeners():
    """项目里**任何** .ps1 的代码都不许用 `GetActiveTcpListeners()`。

    上一轮只修了 status.ps1 和 start.ps1，漏了 restart.ps1 / install.ps1 /
    qq\\启动-小鱼.ps1 —— 结果 restart.bat 仍然假失败（100.7s / 退出码 1）。
    所以这条检查要**扫全仓**，不是逐个点名。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__",
                 "assets", "SnowLuma", "astrbot", "_归档", "vendor", "dist", "build"}
    offenders = []
    for p in root.rglob("*.ps1"):
        if any(part in skip_dirs for part in p.parts):
            continue
        try:
            txt = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        code = "\n".join(
            ln for ln in txt.splitlines() if not ln.lstrip().startswith("#")
        )
        if "GetActiveTcpListeners" in code:
            offenders.append(str(p.relative_to(root)))
    assert not offenders, (
        f"这些脚本还在用 GetActiveTcpListeners()：{offenders}\n"
        f"它在本机只看得到 12 个监听器里的 1 个，会把正常启动报成失败。"
        f"请改成 Get-NetTCPConnection -LocalPort。"
    )


def test_按端口找进程不能加_State_Listen():
    """`Get-NetTCPConnection` 查进程时不能带 `-State Listen`。

    踩过的坑（2026-11-04 第二十一轮）：
    本机有些监听器的 `State` 字段是**空串**（不是 "Listen"），
    带 `-State Listen` 过滤会把它们漏掉。
    实测后果：restart.ps1 第一次「停」漏掉桌宠/网关的 8848、8898，
    要多"强杀一次"才停得掉，restart 时间从 48.9s 变成 64.1s。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__",
                 "assets", "SnowLuma", "astrbot", "_归档", "vendor", "dist", "build"}
    offenders = []
    for p in root.rglob("*.ps1"):
        if any(part in skip_dirs for part in p.parts):
            continue
        try:
            txt = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for ln in txt.splitlines():
            s = ln.lstrip()
            if s.startswith("#"):
                continue
            if "Get-NetTCPConnection" in ln and "-State Listen" in ln:
                offenders.append(f"{p.relative_to(root)}: {s[:70]}")
    assert not offenders, (
        f"这些地方用 Get-NetTCPConnection 时带了 -State Listen：\n" +
        "\n".join("  " + o for o in offenders) +
        "\n本机有些监听器的 State 是空串，带这个过滤会漏掉它们。"
    )


# --------------------------------------------------------------------------- #
# Python 侧也不许用不可靠的端口/反查方式（2026-11-04 第二十二轮）
# --------------------------------------------------------------------------- #
def test_python代码不用netstat反查PID():
    """`tts_breeze.pid_listening_on` 不能用 netstat。

    踩过的坑：本机 `netstat -ano -p TCP` 输出 229 行，
    **完全没有 8848 的 LISTENING 行**（只有 TIME_WAIT），
    于是反查返回 None → 退出时找不到语音引擎 PID →
    **3.5GB 显存释放不掉**。

    实测修后：起 8848 后 `pid_listening_on(8848)` 返回真实 pid（修复前是 None）。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__",
                 "assets", "SnowLuma", "astrbot", "_归档", "vendor", "dist", "build"}
    offenders = []
    for p in root.rglob("*.py"):
        if any(part in skip_dirs for part in p.parts):
            continue
        # 测试文件自己会提到 netstat，跳过
        if p.name.startswith("test_"):
            continue
        try:
            txt = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for ln in txt.splitlines():
            s = ln.lstrip()
            if s.startswith("#"):
                continue
            # 只抓"用 netstat 找 PID"这种，注释和文档提 netstat 不算
            if '"netstat"' in ln or "'netstat'" in ln:
                offenders.append(f"{p.relative_to(root)}: {s[:70]}")
    assert not offenders, (
        "这些地方在调用 netstat（本机看不到 python/node 服务的 LISTENING 行）：\n" +
        "\n".join("  " + o for o in offenders) +
        "\n改用 Get-NetTCPConnection（PowerShell）或 psutil。"
    )


def test_按端口找进程不加State过滤_python侧():
    """Python 里拼的 PowerShell 命令也不能带 `-State Listen`。

    本机有些监听器的 State 是空串，带这个过滤会漏掉它们。
    实测：`desktop/server.py` 的 qq_stop 曾因此找不到 QQ 端口上的进程。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__",
                 "assets", "SnowLuma", "astrbot", "_归档", "vendor", "dist", "build"}
    offenders = []
    for p in root.rglob("*.py"):
        if any(part in skip_dirs for part in p.parts):
            continue
        if p.name.startswith("test_"):
            continue
        try:
            txt = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for ln in txt.splitlines():
            s = ln.lstrip()
            if s.startswith("#"):
                continue
            if "Get-NetTCPConnection" in ln and "-State Listen" in ln:
                offenders.append(f"{p.relative_to(root)}: {s[:70]}")
    assert not offenders, (
        "这些地方用 Get-NetTCPConnection 时带了 -State Listen：\n" +
        "\n".join("  " + o for o in offenders)
    )


def test_install_ps1_检查桌宠素材():
    """`install.ps1` 必须检查 desktop/assets 和 desktop/web/vendor。

    踩过的坑（2026-11-04 第二十四轮）：
    这两个目录**刻意不进 git**（5.8G，见 .gitignore），所以 git clone
    下来的仓库里没有它们。而 install.ps1 原来只查 QQ/Python/端口/key/pip 包，
    **一项都不查素材**。

    缺了会怎样：桌宠窗口照样弹出来，但 canvas 里**什么都没有**（白屏），
    而且没有任何报错 —— 用户只会觉得"这软件坏了"。
    这和当初"只查版本不查 pip 包"是同一类问题：体检给虚假的安全感。
    """
    import re

    p = Path(__file__).resolve().parents[2] / "install.ps1"
    if not p.exists():
        pytest.skip("install.ps1 不存在")
    src = p.read_text(encoding="utf-8-sig")
    # 去掉注释行再判
    code = "\n".join(
        ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "desktop\\assets" in code or "desktop/assets" in code, (
        "install.ps1 应该检查 desktop\\assets（Live2D 素材，不在 git 里）"
    )
    assert "vendor" in code, (
        "install.ps1 应该检查 desktop\\web\\vendor（PixiJS/Live2D 运行时，不在 git 里）"
    )
    # 必须给出"怎么修"的提示，不能只说"缺了"
    assert "clone" in src or "拷贝" in src or "压缩包" in src, (
        "缺素材时要告诉用户怎么办（从原机器拷贝 / 用完整压缩包）"
    )


# --------------------------------------------------------------------------- #
# JS 侧也要扫（2026-11-05 修：之前的扫描漏了整个 desktop/desktop/*.js）
# --------------------------------------------------------------------------- #
def test_JS代码也不许带State_Listen():
    """Electron 主进程拼的 PowerShell 命令同样不能带 `-State Listen`。

    踩过的坑（2026-11-05）：前几轮加「扫全仓」测试时只扫了 `.ps1` / `.py`，
    **漏了整个 `.js`**。而 `desktop/desktop/main.js` 里有两处：

      - `killEngineByPort()` —— 退出时强制杀 8849 的语音引擎释放 3.5GB 显存。
        带 `-State Listen` 会漏判 → **显存释放不掉**。
      - `toggleQq()` —— 关 QQ 托管时按端口找进程。
        漏判 → **托管关不干净，留下孤儿**。

    教训：说「扫全仓」的时候，得真的把会被执行的文件类型都列进去。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    # ⚠ "qq-bridge" 要跳过：它是**已停用的旧方案**（.gitignore 里排除，
    # 第 102 行也注明了「已停用」）。它里面用 netstat 是历史遗留，
    # 但那段代码不会跑，改它没意义 —— 扫它只会制造假警报。
    skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__",
                 "assets", "SnowLuma", "astrbot", "_归档", "vendor",
                 "dist", "build", "probe", "tests", "tts_cache", "qq-bridge"}
    offenders = []
    for p in root.rglob("*.js"):
        if any(part in skip_dirs for part in p.parts):
            continue
        try:
            txt = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for ln in txt.splitlines():
            s = ln.lstrip()
            if s.startswith(("//", "*")):
                continue
            if "Get-NetTCPConnection" in ln and "-State Listen" in ln:
                offenders.append(f"{p.relative_to(root)}: {s[:70]}")
    assert not offenders, (
        "这些 JS 里拼的 PowerShell 命令带了 -State Listen：\n" +
        "\n".join("  " + o for o in offenders) +
        "\n本机有些监听器 State 是空串，带过滤会漏掉它们。"
    )


def test_JS代码也不用GetActiveTcpListeners和netstat():
    """`.js` 里同样不许用 `GetActiveTcpListeners()` 或调 `netstat`。

    理由和 Python / PowerShell 侧完全一样（本机看不到大部分监听器）。
    这条存在的唯一原因：之前的"扫全仓"漏了 `.js`。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    # ⚠ "qq-bridge" 要跳过：它是**已停用的旧方案**（.gitignore 里排除，
    # 第 102 行也注明了「已停用」）。它里面用 netstat 是历史遗留，
    # 但那段代码不会跑，改它没意义 —— 扫它只会制造假警报。
    skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__",
                 "assets", "SnowLuma", "astrbot", "_归档", "vendor",
                 "dist", "build", "probe", "tests", "tts_cache", "qq-bridge"}
    offenders = []
    for p in root.rglob("*.js"):
        if any(part in skip_dirs for part in p.parts):
            continue
        try:
            txt = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for ln in txt.splitlines():
            s = ln.lstrip()
            if s.startswith(("//", "*")):
                continue
            if "GetActiveTcpListeners" in ln or '"netstat"' in ln or "'netstat'" in ln:
                offenders.append(f"{p.relative_to(root)}: {s[:70]}")
    assert not offenders, (
        "这些 JS 里用了不可靠的端口探测：\n" +
        "\n".join("  " + o for o in offenders)
    )


# --------------------------------------------------------------------------- #
# 轮盘菜单的文案不能和实际行为脱节（2026-11-05）
# --------------------------------------------------------------------------- #
def _pet_js() -> str:
    from pathlib import Path
    p = Path(__file__).resolve().parents[2] / "desktop" / "web" / "pet.js"
    if not p.exists():
        pytest.skip("pet.js 不存在")
    return p.read_text(encoding="utf-8-sig")


def test_重启文案不说90秒():
    """「重启」的 hint 不能写 90 秒。

    踩过的坑（2026-11-05，主人问「这三个按钮是不是过时了」时查出来的）：
    hint 一直写「大概 90 秒」，而实测 `restart.bat` 只要 **~49 秒**。
    90 秒其实是 `status.ps1` 的**等待上限**，不是真实耗时。

    差一倍会让人怀疑「怎么这么快就好了，是不是没重启」。
    """
    src = _pet_js()
    import re
    m = re.search(r'label:\s*"重启".*?hint:\s*"([^"]+)"', src, re.S)
    assert m, "找不到「重启」的 hint"
    hint = m.group(1)
    assert "90" not in hint, (
        f"「重启」还写着 90 秒（实测 ~49 秒，90 是等待上限不是耗时）：{hint}"
    )
    assert "50" in hint or "49" in hint, f"应该写实测耗时，实际：{hint}"


def test_退出文案要说清会停QQ():
    """「退出」的 hint 必须提到**停 QQ 托管**。

    实际链路：`quitAll() -> stopHeavy() -> POST /api/shutdown -> qq_stop()`
    —— QQ 那套会被停掉。

    而原来 hint 只写「停语音引擎 + 关闭 DSH」，**漏了 QQ**。
    这个差别对用户很大：他只是想关桌宠，结果 QQ 机器人也下线了。
    """
    src = _pet_js()
    import re
    m = re.search(r'label:\s*"退出".*?hint:\s*"([^"]+)"', src, re.S)
    assert m, "找不到「退出」的 hint"
    hint = m.group(1)
    assert "QQ" in hint, (
        f"「退出」没说会停 QQ 托管，但实际会（stopHeavy -> /api/shutdown -> qq_stop）：{hint}"
    )


def test_退出二次确认也要说清后果():
    """二次确认页的「确定」按钮 hint 同样要提 QQ。

    这是**最后一次确认**，后果写不全等于没提示。
    """
    src = _pet_js()
    import re
    m = re.search(r'label:\s*"确定".*?hint:\s*"([^"]+)"', src, re.S)
    assert m, "找不到「确定」的 hint"
    hint = m.group(1)
    assert "QQ" in hint, f"二次确认没说清会停 QQ：{hint}"
    assert "显存" in hint, f"二次确认要说清会释放显存：{hint}"


# --------------------------------------------------------------------------- #
# 三个动作的边界（2026-11-05 按主人给的规格固定）
# --------------------------------------------------------------------------- #
# 主人原话：
#   启动  全部拉起来，同时看 DSH 和语音引擎有没有起来；
#         这两个已经启动了，启动脚本要跳过，不然重复启动冲突。
#   重启  除了语音引擎和 DSH 以外的其他组件全关了，然后再重新打开。
#   退出  除了 DSH 外的其他组件全部关了，包括 QQ 机器人。
# --------------------------------------------------------------------------- #
def _main_js() -> str:
    return _js_file("desktop/desktop/main.js")


def test_退出不关DSH():
    """`quitAll()` 不许调 `killDsh()`。

    规格：退出 = 除 DSH 外全关（含 QQ），**DSH 保留**。

    以前是反的：`quitAll` 会主动 `killDsh()`，等于「我只是想关桌宠，
    结果主人的 DeepSeek Harness 也被杀了」。DSH 是主人自己的应用，
    不是小鱼的组件，没理由顺手关掉。

    2026-11-05 按规格改成保留。
    """
    src = _main_js()
    # 找到 quitAll 函数体
    import re
    m = re.search(r"async function quitAll\(\).*?\n\}", src, re.S)
    assert m, "找不到 quitAll"
    body = m.group(0)
    # 去掉注释再判（注释里会提到 killDsh 解释为什么不调）
    code = "\n".join(
        ln for ln in body.splitlines() if not ln.lstrip().startswith("//")
    )
    assert "killDsh(" not in code, (
        "quitAll 还在调 killDsh() —— 按规格退出必须保留 DSH。\n"
        "（注释里说明原因没问题，但代码不能真调）"
    )


def test_重启不碰语音引擎和DSH():
    """`restart.bat` / `restart.ps1` 不能杀语音引擎和 DSH。

    规格：重启 = 除语音引擎和 DSH 外全关，再重开。

    语音引擎是 3.5GB 模型，重载要 10~30 秒；DSH 是主人自己的应用。
    两个都留着给新实例复用，重启才快（实测 ~50 秒而不是几分钟）。
    """
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    ps1 = (root / "restart.ps1").read_text(encoding="utf-8-sig")

    # 不能出现的：把 8849（语音引擎端口）或 breeze 进程纳入停止范围
    assert "8849" not in ps1.replace("8850", ""), (
        "restart.ps1 里出现了 8849（语音引擎端口）—— 按规格重启不能碰语音引擎"
    )
    code_lines = [
        ln for ln in ps1.splitlines()
        if not ln.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)
    assert "breeze" not in code.lower(), (
        "restart.ps1 的代码里出现了 breeze（语音引擎）—— 按规格重启不能碰它"
    )
    # 必须明确声明保留
    assert "保持运行" in ps1 or "不重启" in ps1, (
        "restart.ps1 应该明确写出「语音引擎保持运行」"
    )


def test_启动会跳过已在跑的DSH和语音引擎():
    """启动脚本必须能跳过已经在跑的 DSH 和语音引擎。

    规格：这两个已经启动了，启动脚本要跳过 —— 不然重复启动冲突。

    - DSH：`start.ps1` 按 `ops.dsh_exe` 的 **ExecutablePath 精确匹配**找进程，
      找到就跳过（不能按进程名，会命中别的安装位置的同名程序）。
    - 语音引擎：`tts_breeze.start()` 三分支 ——
      ① 自己拉起的还健康 -> return；② 8849 上有引擎 -> adopt 复用；
      ③ 正在加载 -> 等它就绪（不 spawn 第二个，否则两个实例各吃 3.5GB）
    """
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]

    # DSH：start.ps1 里要有"已在跑就跳过"
    start_ps1 = (root / "start.ps1").read_text(encoding="utf-8-sig")
    assert "Get-DshProcesses" in start_ps1, "start.ps1 应该有 DSH 进程探测"
    assert "已经在跑" in start_ps1, "start.ps1 应该在 DSH 已在跑时跳过"
    # 必须按 ExecutablePath 精确匹配（注释里说明了原因）
    assert "ExecutablePath" in start_ps1, (
        "start.ps1 探测 DSH 要按 ExecutablePath 精确匹配，"
        "按进程名会误判成「已经在跑」而永不启动"
    )

    # 语音引擎：tts_breeze.start 要有"复用已在运行的实例"
    breeze = (root / "desktop" / "tts_breeze.py").read_text(encoding="utf-8-sig")
    assert "复用已在运行的语音引擎" in breeze or "_adopt_existing" in breeze, (
        "tts_breeze.start() 应该能复用已在 8849 上运行的引擎（不 spawn 第二个）"
    )
    assert "RECALL" not in breeze  # 防串行断言：确认读的是对的文件


def _js_file(rel: str) -> str:
    """读一个 JS 文件（相对仓库根）。"""
    from pathlib import Path
    p = Path(__file__).resolve().parents[2] / rel
    if not p.exists():
        pytest.skip(f"{rel} 不存在")
    return p.read_text(encoding="utf-8-sig")


def test_退出文案说DSH保留():
    """「退出」的 hint 要明确「DSH 保留」，不能写「关闭 DSH」。"""
    src = _pet_js()
    import re
    m = re.search(r'label:\s*"退出".*?hint:\s*"([^"]+)"', src, re.S)
    assert m, "找不到「退出」的 hint"
    hint = m.group(1)
    assert "DSH" in hint, f"「退出」应该说明 DSH 怎么处理：{hint}"
    assert "保留" in hint, f"按规格 DSH 是保留的，hint 要写清：{hint}"
    assert "关闭 DSH" not in hint, f"不能写「关闭 DSH」（规格是保留）：{hint}"
