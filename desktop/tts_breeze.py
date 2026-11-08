"""Breeze-TTS-2.cpp 语音引擎（Vulkan GPU + GGUF，3.5GB 模型常驻子进程）。

和 FastVITS 同接口：start() / ready / speak_wav(text) / stop()，server.py 里 _launch_tts()
按 config.json 的 tts_engine 字段选择加载哪个。

实现：spawn breeze-server.exe 子进程（HTTP server，模型加载一次），
调 /v1/audio/speech（OpenAI JSON 模式，response_format=wav）拿标准 16-bit PCM WAV，
直接写到 TTS_OUTDIR 返回路径。前端和 FastVITS 完全无感。

线程安全：breeze 单线程生成，speak_wav 用锁串行化。
409 busy：引擎忙时退避重试（最多 3 次，1s/2s/4s）。
崩溃重拉：server.py 的 _tts_supervisor() 3s 轮询 tts.ready，掉了自动重拉。

引擎归属（重要，见 start()）：
    Electron 的 serverProc.kill() 在 Windows 上是 TerminateProcess，server.py 的
    finally: tts.stop() 不会执行 —— 于是 breeze-server.exe 会变成孤儿，继续占着
    8849/8850 和 3.5GB 显存。下一次启动时新 exe 因为端口被占立刻退出，而旧的
    start() 只轮询 /health、看到「健康」就报成功，于是「看着起来了其实没引擎」。
    现在的策略是**复用**而不是杀掉重建：8849 上已有健康引擎就 adopted=True、
    不 spawn、stop() 时也不 kill，让它当半常驻服务存在。重载一次 3.5GB 模型
    要 10~30s，复用便宜得多。
    例外是 stop(force=True)：那是「退出」专用的路径，用户明确要释放显存，
    这时才不管 adopted 与否，直接按 adopted_pid / 端口反查 PID 强杀。

音色稳定（重要）：
    voice_id 为空时走 **voice design**——按 instruction 每次现编一个声音，
    描述相同、seed 相同，换一句话音色照样漂（实测同一 seed 下长时平均频谱相似度
    只有 0.88），听感就是「每次都换一个人」。
    voice_id 指向一个 saved voice 时走 **voice clone**——音色由参考音频锁死，
    instruction 退化为「语气方向」（快慢、情绪），不再改变是谁在说话。
    存 voice 用 probe/save_voice.py，挑参考音频用 probe/make_voice_candidates.py。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

# 必须和 tts_fastvits 一样在 sys.path 里被 import（server.py 会 sys.path.insert）
# 但这里不依赖任何 FastVITS 的东西，纯标准库。

# 分片：Breeze 是自回归逐帧生成，一次喂太长的文本有三重代价
# （显存峰值高、总耗时超线性、单次请求超时风险大）。实测 64 字单次 45.9s，
# 拆成 4×16 字只要 30s。所以长文本按句号切开分别合成，再缝回一条 WAV。
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])")
_CLAUSE_SPLIT = re.compile(r"(?<=[，,、：:])")


def split_for_tts(text: str, max_chars: int = 28) -> list[str]:
    """把文本切成若干不超过 max_chars 的片段，优先在句子边界切。

    规则：
      1. 先按句末标点分句，逐句累加，凑到 max_chars 就封一片；
      2. 单句本身就超长时，退到逗号/顿号再切；
      3. 连逗号都切不开（比如一长串没有标点的字）才硬切。
    """
    clean = re.sub(r"\s+", "", (text or "").strip())
    if not clean:
        return []
    if len(clean) <= max_chars:
        return [clean]

    # 第一步：分句
    sentences = [s for s in _SENT_SPLIT.split(clean) if s]
    # 第二步：超长句退到逗号
    units: list[str] = []
    for s in sentences:
        if len(s) <= max_chars:
            units.append(s)
            continue
        clauses = [c for c in _CLAUSE_SPLIT.split(s) if c]
        for c in clauses:
            if len(c) <= max_chars:
                units.append(c)
            else:
                for i in range(0, len(c), max_chars):
                    units.append(c[i:i + max_chars])

    # 第三步：把小句拼回接近 max_chars 的片，避免切成碎末
    chunks: list[str] = []
    cur = ""
    for u in units:
        if cur and len(cur) + len(u) > max_chars:
            chunks.append(cur)
            cur = u
        else:
            cur += u
    if cur:
        chunks.append(cur)
    return chunks


def _parse_wav(wav: bytes) -> tuple[bytes, bytes, int, int, int]:
    """拆 WAV，返回 (fmt 段原始字节, data 段原始字节, 采样率, 声道数, 位深)。"""
    if len(wav) < 44 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise ValueError("不是合法的 RIFF/WAVE 数据")
    pos = 12
    fmt: bytes | None = None
    data: bytes | None = None
    while pos + 8 <= len(wav):
        cid = wav[pos:pos + 4]
        size = int.from_bytes(wav[pos + 4:pos + 8], "little")
        payload = wav[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = payload
        elif cid == b"data":
            data = payload
        pos += 8 + size + (size & 1)  # RIFF 要求块按偶数字节对齐
    if fmt is None or data is None or len(fmt) < 16:
        raise ValueError("WAV 缺少 fmt/data 段")
    channels = int.from_bytes(fmt[2:4], "little")
    sample_rate = int.from_bytes(fmt[4:8], "little")
    bits = int.from_bytes(fmt[14:16], "little")
    return fmt, data, sample_rate, channels, bits


def _build_wav(fmt: bytes, data: bytes) -> bytes:
    """用 fmt 段和拼好的 PCM 数据重建一个干净的 WAV。"""
    fmt_chunk = b"fmt " + len(fmt).to_bytes(4, "little") + fmt
    if len(fmt) & 1:
        fmt_chunk += b"\x00"
    data_chunk = b"data" + len(data).to_bytes(4, "little") + data
    if len(data) & 1:
        data_chunk += b"\x00"
    body = b"WAVE" + fmt_chunk + data_chunk
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def _no_window() -> int:
    """Windows 下隐藏子进程控制台窗口（其它平台为 0）。"""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_console(args: list[str], timeout: float = 10, check: bool = False) -> str:
    """跑一个控制台命令并返回 stdout 文本。

    必须显式指定编码：中文 Windows 上 netstat / tasklist 输出的是 GBK（mbcs），
    用 subprocess 的默认 UTF-8 解码会在读取线程里抛 UnicodeDecodeError，
    stdout 直接变成空串 —— 表现就是「明明有进程，却查不到」。

    check=True 时非零退出码抛 RuntimeError。默认 False 是为了兼容原有调用方：
    大多数地方只关心「有没有输出」，而 netstat / tasklist 偶尔的怪退出码不值得中断。
    但**进程枚举不能这么糊过去** —— 命令失败和「真的没有进程」必须能区分，
    否则加载中的引擎会被漏掉（见 pids_with_exe_path）。
    """
    base = dict(capture_output=True, timeout=timeout, creationflags=_no_window())
    try:
        out = subprocess.run(args, text=True, encoding="mbcs", errors="replace", **base)
    except LookupError:  # 非 Windows 没有 mbcs
        out = subprocess.run(args, text=True, errors="replace", **base)
    if check and out.returncode != 0:
        detail = (out.stderr or out.stdout or "").strip().replace("\n", " ")[:200]
        raise RuntimeError(f"{Path(args[0]).name} 退出码 {out.returncode}: {detail}")
    return out.stdout or ""


def pid_listening_on(port: int) -> int | None:
    """查监听 port 的进程 PID，查不到返回 None。

    复用来的引擎（adopted=True）我们手上没有 Popen 句柄，只能按端口反查。
    退出时要用它把 3.5GB 显存真正释放掉 —— 所以这个方法**必须可靠**。

    ⚠ 2026-11-04（第二十二轮）修：**不能用 netstat**。

    实测：本机上 `netstat -ano -p TCP` 输出 229 行，但
    **完全没有 8848 的 LISTENING 行**（只有一条 TIME_WAIT），
    于是这个函数返回 None → 退出时找不到引擎 PID →
    **3.5GB 显存释放不掉**。
    （同一个坑在第七轮 stop.cmd、第二十/二十一轮启动脚本里都踩过。）

    改用 PowerShell 的 `Get-NetTCPConnection` —— 它是这台机器上唯一可靠的。
    ⚠ 不按 `-State` 过滤：某些监听器的 State 字段是**空串**。
    """
    ps = (
        f"$c = Get-NetTCPConnection -LocalPort {int(port)} "
        f"-ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.OwningProcess -ne 0 }} | "
        f"Select-Object -First 1; "
        f"if ($c) {{ $c.OwningProcess }}"
    )
    try:
        text = _run_console(["powershell", "-NoProfile", "-Command", ps])
    except Exception:  # noqa: BLE001
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.isdigit():
            pid = int(line)
            if pid > 0:
                return pid
    return None
    return None


def exe_name_of(pid: int) -> str:
    """查 PID 对应的可执行文件名（小写），查不到返回空串。

    杀进程前用它确认「端口上这个 PID 确实是语音引擎」，避免端口被别的程序
    占用时误杀别人的进程。
    """
    try:
        text = _run_console(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"])
    except Exception:  # noqa: BLE001
        return ""
    lines = text.strip().splitlines()
    if not lines or not lines[0].startswith('"'):
        return ""
    return lines[0].split('","')[0].strip('"').lower()


def pids_with_exe_path(exe: str) -> list[int] | None:
    """按可执行文件**全路径**精确匹配进程，返回 PID 列表。

    返回值的三种含义必须分清：
        [1, 2]  查到了，这些进程在跑
        []      查询成功，确实没有同款进程
        None    查询**失败**（PowerShell 起不来 / WMI 冷启动超时 / 被杀软拦）

    用途见 start() 的第 ③ 步：引擎加载模型时**先吃 3.5GB 显存、后 bind 端口**，
    所以「端口上没人」并不代表「没有引擎在启动」。这时候要是再 spawn 一个，
    两个实例各占 3.5GB（实测 8188MiB 的卡会直接顶满），所以必须先按路径找同款进程。
    按名字找不行：别的安装位置的 breeze-server 会被误认。

    失败时返回 None 而不是 []：早先两者都返回 []，等于让调用方把「查不到」
    当成「没人」，PowerShell 一失效闸门就静默 fail-open —— 加载窗口内照样
    spawn 第二个实例。调用方现在必须显式处理 None。
    """
    if not exe:
        return []
    # WMI 的 ExecutablePath 一律是「盘符:\」全反斜杠，传正斜杠会一个都匹配不到
    quoted = os.path.normpath(str(exe)).replace("'", "''")
    # 注意：这里**故意不加** `-ErrorAction SilentlyContinue`。
    # WMI 自身报错（类不可用 / 服务没起来）时它会把错误吞掉、powershell 退出码仍是 0，
    # check=True 就挡不住 → 返回 [] = 「确实没人」→ 加载窗口内立刻 spawn，退回旧 bug。
    # 去掉之后：WMI 报错 → 非零退出 → 抛 → 返回 None（=「查不到，别当没人」）。
    # 真没有同款进程时 Get-CimInstance 不报错，正常路径不受影响。
    script = (
        f"$exe = '{quoted}'; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.ExecutablePath -eq $exe } | "
        "ForEach-Object { $_.ProcessId }"
    )
    try:
        text = _run_console(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=20,
            check=True,
        )
    except Exception:  # noqa: BLE001
        return None
    pids: list[int] = []
    for token in text.split():
        token = token.strip()
        if token.isdigit():
            pids.append(int(token))
    return pids


class Breeze:
    """常驻 Breeze TTS 引擎。"""

    def __init__(self, server: str, model: str, outdir: str,
                 port: int = 8849, instruction: str = "", voice_id: str = "",
                 cfg_scale: float = 1.0, seed: int = 42,
                 max_new_tokens: int = 0, warmup: bool = False,
                 chunk_chars: int = 28, chunk_gap_ms: int = 250,
                 load_wait: float = 45.0):
        self.server = server
        self.model = model
        self.port = port
        self.outdir = Path(outdir)
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.instruction = instruction or "Speak clearly and naturally."
        self.voice_id = voice_id
        self.voice_ok: bool | None = None   # None=未知，True/False=启动时核对结果
        self.cfg_scale = cfg_scale
        self.seed = seed
        self.max_new_tokens = max_new_tokens
        self.warmup = warmup
        # 分片合成：单次请求短一点，显存峰值低、总耗时也更短
        self.chunk_chars = max(8, int(chunk_chars or 28))
        self.chunk_gap_ms = max(0, int(chunk_gap_ms or 0))
        # 「已经有一个引擎在加载」时最多等它多久（秒）。见 start() 第 ③ 步：
        # 冷启动加载 10~30s，这期间端口还没 bind，不能当成「没人」直接 spawn。
        # 默认 45 而不是 30：30 秒和真实冷加载上限的余量是 0，卡在边界上会把一个
        # 快就绪的引擎判成卡死杀掉，再重载一次又是 10~30s。多等 15 秒远好过杀错。
        self.load_wait = max(0.0, float(load_wait if load_wait is not None else 45.0))

        self.proc: subprocess.Popen | None = None
        # 「复用的别人家的引擎」标记。见 start()：8849 上已经有一个健康的引擎
        # （典型场景是上一个 server.py 被 Electron 的 TerminateProcess 杀掉、
        # 它 spawn 的 breeze-server 活下来成了孤儿），就不再新建实例，直接复用。
        # adopted=True 时 self.proc 一定是 None —— 那不是我们的进程，不能 kill。
        self.adopted: bool = False
        self.adopted_pid: int | None = None
        self._lock = threading.Lock()
        self._counter = 0
        # 引擎的 stderr 里有每句的分段耗时（time to first audio / depth decode 之类），
        # 以前丢进 DEVNULL 就等于瞎跑：慢的时候完全看不出慢在哪一段。
        self.logfile = Path(outdir).parent / "breeze-server.log" if outdir else None

    @property
    def _url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ------------------------------------------------------------------ 健康
    def _health(self) -> dict | None:
        """GET /health，返回解析后的 JSON；没起来返回 None。"""
        try:
            with urllib.request.urlopen(self._url + "/health", timeout=3) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

    def _healthy(self) -> bool:
        """引擎真的就绪了吗。

        唯一口径：`/health` 回 `status == "ok"`。
        `ready` 以前用的是 `bool(self._health())` —— 只要 HTTP 通了就算就绪，
        引擎回 `{"status":"loading"}`（模型还在加载）时会被误判成可用，
        于是 `/api/tts` 拿到一个半启动的引擎。这里统一到 start() 用的那个判据。
        """
        health = self._health()
        return bool(isinstance(health, dict) and health.get("status") == "ok")

    def voices(self) -> list[str]:
        """GET /v1/voices，返回可用声线 id 列表；查不到返回空列表。"""
        try:
            with urllib.request.urlopen(self._url + "/v1/voices", timeout=5) as resp:
                items = json.loads(resp.read().decode("utf-8"))
            return [v.get("id", "") for v in items if isinstance(v, dict)]
        except Exception:
            return []

    def check_voice(self, log=print) -> bool:
        """核对配的 voice_id 是否真的在库里，避免整场对话全部 404。"""
        if not self.voice_id:
            self.voice_ok = True   # 没配就是 design 模式，不算错
            return True
        names = self.voices()
        self.voice_ok = self.voice_id in names
        if self.voice_ok:
            log(f"[breeze] 声线 {self.voice_id} 已就位（克隆模式，音色固定）")
        else:
            log(f"[breeze] 警告：声线 {self.voice_id} 不在库里（现有 {names or '空'}），"
                f"合成会 404。用 probe/save_voice.py 建一个，或把 config 的 "
                f"breeze.voice_id 清空退回 design 模式（音色会漂）")
        return self.voice_ok

    # ------------------------------------------------------------------ 生命周期
    def _log_handle(self):
        """把引擎输出重定向到文件；打不开就退回 DEVNULL，绝不因此起不来。"""
        if not self.logfile:
            return subprocess.DEVNULL
        try:
            self.logfile.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.logfile, "ab", buffering=0)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            fh.write(f"\n=== {stamp} 启动 {Path(self.server).name} ===\n".encode())
            return fh
        except OSError:
            return subprocess.DEVNULL

    @staticmethod
    def _vram() -> tuple[int, int] | None:
        """(已用, 总量) MiB。没有 nvidia-smi 就是 None。"""
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            used, total = out.stdout.strip().splitlines()[0].split(",")
            return int(float(used)), int(float(total))
        except Exception:  # noqa: BLE001
            return None

    def _check_vram(self, log=print) -> None:
        """显存放不下模型时报警。

        这是「明明在用 GPU 却慢十倍」的头号原因：权重没进显存，每个 token 都要
        从内存经 PCIe 重读一遍 3.5GB。表现为显卡满频、满载、功耗却只有三四十瓦。
        引擎自己不会报这个，只在 /v1/audio/speech 的耗时里体现。
        """
        vram = self._vram()
        try:
            need = int(Path(self.model).stat().st_size / 2 ** 20) + 300
        except OSError:
            return
        if not vram:
            return
        used, total = vram
        free = total - used
        if free < need:
            log(f"[breeze] ⚠ 显存不够：{used}/{total} MiB 已用，只剩 {free}，"
                f"模型要 {need} —— 权重会退到内存，每帧经 PCIe 重读，慢十倍以上。"
                f"关掉壁纸引擎/浏览器/QQ音乐这类吃显存的程序再重启语音。")
        else:
            log(f"[breeze] 显存 {used}/{total} MiB，可用 {free}，够放下模型（{need}）")

    def _instance_hint(self, health: dict | None) -> str:
        """从 /health 响应里尽量抠一个实例标识，抠不到就返回空串。

        引擎当前只回 `{"status","ws_port"}`，没有 pid；所以这里不假设它一定有。
        有 `pid` / `instance` / `model` 就用，没有就空着 —— 归属判断退回到
        「端口探测 + adopted 标记」，不做过度设计。
        """
        if not isinstance(health, dict):
            return ""
        for field in ("pid", "instance", "instance_id", "model"):
            value = health.get(field)
            if value not in (None, ""):
                return f"{field}={value}"
        return ""

    def _wait_healthy(self, timeout: float) -> bool:
        """轮询 /health，最多等 timeout 秒；等到 status == "ok" 返回 True。"""
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            if self._healthy():
                return True
            time.sleep(1)
        return False

    # 下面两个探测是 start() 判断「端口上/系统里是不是已经有个引擎」的唯一入口。
    # 抽成实例方法是为了可替换：单测里把它俩改成 lambda 就完全脱离真实进程表，
    # 否则跑测试会去查真实的 8849 和真实的 PowerShell。
    def _port_occupant(self) -> int | None:
        """监听本引擎端口的 PID（没有返回 None）。"""
        return pid_listening_on(self.port)

    def _same_exe_pids(self) -> list[int] | None:
        """同可执行文件路径的其它进程 PID（加载中的实例还没 bind 端口，只能靠它发现）。

        查询失败返回 None（不是 []），见 pids_with_exe_path 的说明。
        """
        pids = pids_with_exe_path(self.server)
        if pids is None:
            return None
        return [p for p in pids if p != os.getpid()]

    def _wait_port_free(self, timeout: float = 10.0) -> bool:
        """等端口不再被监听（taskkill 是异步的），超时返回 False。"""
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            if self._port_occupant() is None:
                return True
            time.sleep(0.5)
        return self._port_occupant() is None

    def _adopt_existing(self, pid: int | None, health: dict | None, log=print) -> None:
        """把「已经存在的引擎」记成复用状态（不 spawn、不 kill）。

        health 由调用方传进来，**不要在这里再查一次** —— 多查一次会打乱
        /health 的调用节奏（单测按脚本返回，线上也白多一次 HTTP）。
        """
        health = health if isinstance(health, dict) else {}
        self.adopted = True
        self.proc = None
        self.adopted_pid = health.get("pid") if isinstance(health.get("pid"), int) else pid
        self.check_voice(log=log)

    def start(self, timeout: float = 120.0, log=print) -> None:
        """（重新）创建 Breeze 实例并等它就绪。调用方负责加锁。

        顺序很重要：**先看 8849 上有没有已经活着的引擎**。
        以前只 spawn 再轮询 /health，而那个 /health 可能是上一个实例留下的孤儿
        在应答 —— 新起的 exe 因为端口被占立刻退出，start() 却报成功。
        结果就是「看着起来了，其实没引擎」，/api/tts 永远 503。
        """
        # ① 我们自己拉起的进程还活着且健康：什么都不用做
        if self.proc and self.proc.poll() is None and self._healthy():
            return

        # ② 8849 上有一个不认识的引擎在跑：复用它，绝不 spawn、绝不 kill。
        #    3.5GB 模型重载一次要 10~30s，复用比杀掉重建划算得多。
        #    注意这里用 _health() 而不是 _healthy()：需要拿到 health 体里的
        #    实例标识来打日志，但判定仍然只认 status == "ok"。
        health = self._health()
        if health and health.get("status") == "ok":
            hint = self._instance_hint(health)
            self._adopt_existing(None, health, log=log)
            log(
                f"[breeze] 复用已在运行的语音引擎（PID={self.adopted_pid or '未知'}"
                f"{'，' + hint if hint else ''}），未新建实例"
            )
            return

        # ③ 没有健康应答 ≠ 没人。两种「有人在」的情况都必须先处理，绝不能直接 spawn：
        #    a) 引擎正在加载模型 —— 日志顺序是 `loading <model>.gguf` → `listening on 8849`，
        #       也就是**先吃掉 3.5GB 显存、几十秒后才 bind 端口**。这期间 /health 连不上
        #       或回 status=loading，端口也还是空的。此时 spawn 一个新的，两个实例各占
        #       3.5GB（8188MiB 的卡直接顶满，实测会反复尖峰接近 OOM），新 exe 最后还会
        #       因为端口被占而退出。
        #    b) 上次的实例卡死了（端口在监听但不应答）—— 同样不能 spawn。
        #    判定方式：端口反查 + 按可执行文件全路径匹配（加载中的实例还没 bind，只能靠后者）。
        occupant = self._port_occupant()
        occupant_name = exe_name_of(occupant) if occupant else ""
        if occupant and "breeze" not in occupant_name:
            # 端口被别的程序占着：spawn 出来也绑不上，只会白吃 3.5GB 显存再退出。
            raise RuntimeError(
                f"端口 {self.port} 被非引擎进程占用"
                f"（PID={occupant}，{occupant_name or '查不到进程名'}），不启动新实例"
            )

        pending = self._same_exe_pids()
        if pending is None:
            # 枚举失败（PowerShell 起不来 / WMI 冷启动超时 / 被杀软拦）。
            # 这时「查不到」绝不等于「没人」：先重试一次，还失败就告警并给一个宽限，
            # 让正在加载的实例有机会 bind 端口被我们接住，避免白白双开 3.5GB。
            pending = self._same_exe_pids()
            if pending is None:
                log(
                    "[breeze] ⚠ 无法枚举同款引擎进程（PowerShell/WMI 查询失败）。"
                    "加载中的实例可能被漏掉，先等端口出现再决定是否新建"
                )
                pending = []
                # 端口上已经有人占着（occupant 非 None）时，「有没有引擎」根本不需要枚举，
                # 别在这儿白等一轮 load_wait —— 下面 `if pending:` 那段会等并接管它。
                # 枚举失败时 load_wait 默认 45s，白等两轮就是一分半的哑巴期。
                if not occupant and self._wait_healthy(self.load_wait):
                    health = self._health() or {}
                    self._adopt_existing(self._port_occupant(), health, log=log)
                    log(
                        f"[breeze] 端口上出现了引擎（PID={self.adopted_pid or '未知'}），"
                        "复用，未新建进程"
                    )
                    return
        if occupant and occupant not in pending:
            pending.append(occupant)
        pending.sort()

        if pending:
            pids_text = ", ".join(str(p) for p in pending)
            log(
                f"[breeze] 已有引擎进程（PID={pids_text}）但还没就绪，"
                f"等它就绪（最多 {self.load_wait:.0f}s）…"
            )
            if self._wait_healthy(self.load_wait):
                health = self._health() or {}
                # 认端口持有者，不要用 pending[0]（那是最小 PID）：同路径可能同时
                # 有两个进程（老的卡死 + 新的刚起），只有监听端口那个在真干活，
                # 退出时按 adopted_pid 强杀，认错了就会杀错、真引擎留活占着 3.5GB。
                self._adopt_existing(self._port_occupant() or pending[0], health, log=log)
                log(f"[breeze] 等到了，复用这个实例（PID={self.adopted_pid or pids_text}），未新建进程")
                return
            # 等不到 → 判定卡死，清掉再重建（同样要先过「名字像引擎」的闸门）
            log(f"[breeze] 等了 {self.load_wait:.0f}s 仍未就绪，判定为卡死，终止后重建")
            for pid in pending:
                name = exe_name_of(pid)
                if not name or "breeze" not in name:
                    log(f"[breeze] PID {pid}（{name or '已消失'}）不是语音引擎，跳过")
                    continue
                try:
                    # 走 _run_console：taskkill 输出是 GBK，默认 UTF-8 解码会炸
                    _run_console(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=20)
                    log(f"[breeze] 已终止卡死的引擎 PID={pid}")
                except Exception as exc:  # noqa: BLE001
                    log(f"[breeze] 终止 PID {pid} 失败：{exc}")
            if not self._wait_port_free(10.0):
                raise RuntimeError(
                    f"端口 {self.port} 在终止旧引擎后仍被占用，不启动新实例"
                )

        # ④ 真的没有引擎：按原逻辑拉起一个（这时候才归我们管）
        #    但 occupant 是进 ③ 时抓的快照，端口有可能在这几秒里才被 bind
        #    （真引擎几乎不可达，但重探一次只要一次 netstat，成本极低）。
        #    不重探就会在「刚好这时候 bind 上」的瞬间再 spawn 一个，白吃 3.5GB。
        late = self._port_occupant()
        if late:
            log(f"[breeze] 端口 {self.port} 刚出现监听者（PID={late}），按已存在处理")
            if self._wait_healthy(self.load_wait):
                health = self._health() or {}
                self._adopt_existing(late, health, log=log)
                log(f"[breeze] 复用这个实例（PID={self.adopted_pid or late}），未新建进程")
                return
            raise RuntimeError(
                f"端口 {self.port} 在启动前被 PID={late} 占用且始终未就绪，不启动新实例"
            )
        self.adopted = False
        self.adopted_pid = None
        # ⚠ 2026-11-04（第二十三轮）：spawn 前先**自己检查路径**，给出可操作的提示。
        #
        # 原来不检查，`subprocess.Popen` 直接抛
        #     FileNotFoundError: [WinError 2] 系统找不到指定的文件
        # 这个报错**不说哪个文件、也不说怎么修**。而最常见的触发场景恰恰是
        # "换了台机器"（`desktop/config.json` 里的 `breeze_dir` 是绝对路径），
        # 用户看到 WinError 2 完全不知道该改什么。
        server_path = Path(self.server)
        model_path = Path(self.model)
        if not server_path.exists():
            raise FileNotFoundError(
                f"语音引擎可执行文件不存在：{server_path}\n"
                f"  → 检查 desktop\\config.json 里的 breeze_dir 是不是指向了别的机器/别的盘\n"
                f"  → breeze_server 应该是 <breeze_dir>\\breeze-server.exe"
            )
        if not model_path.exists():
            raise FileNotFoundError(
                f"语音模型文件不存在：{model_path}\n"
                f"  → 检查 desktop\\config.json 里的 breeze_model（默认 breeze-tts-2-q8_0.gguf）\n"
                f"  → 模型应该在 <breeze_dir>\\models\\ 下"
            )

        log(f"[breeze] 启动 {server_path.name}（模型 {model_path.name}，"
            f"3.5GB 加载 + warmup 约 10~30s）…")
        self._check_vram(log=log)
        t0 = time.time()
        args = [self.server, self.model,
                "--host", "127.0.0.1", "--port", str(self.port), "--no-warmup"]
        # --no-warmup 省 2s 启动，但首句多 300ms；桌宠一句就几百字，可接受
        if self.warmup:
            args = [a for a in args if a != "--no-warmup"]
        self.proc = subprocess.Popen(
            args,
            cwd=os.path.dirname(self.server),
            stdout=self._log_handle(),
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        while time.time() - t0 < timeout:
            if self.proc.poll() is not None:
                # 走到这里通常是「端口被别的进程占了」：刚 spawn 的 exe 绑不上端口
                # 立刻退出。把话说明白，别让人以为模型加载失败。
                raise RuntimeError(
                    f"breeze-server 退出，code={self.proc.returncode}"
                    f"（端口 {self.port} 可能已被其他实例占用）"
                )
            h = self._health()
            if h and h.get("status") == "ok":
                log(f"[breeze] 就绪 {self._url}（{time.time()-t0:.1f}s，"
                    f"ws_port={h.get('ws_port')}）")
                self.check_voice(log=log)
                return
            time.sleep(1)
        raise RuntimeError("breeze-server 启动超时（模型还在加载或 GPU 初始化慢）")

    @property
    def ready(self) -> bool:
        """引擎可用吗。

        adopted 的情况下 self.proc 是 None，所以不能再用「进程活着」当条件，
        否则 server.py 的 _tts_supervisor 会以为掉线，每 5~60 秒白拉一个必死的
        exe。复用的引擎只按 /health 判断 —— 它掉线了 ready 自然变 False，
        下一轮 supervisor 就会正常拉起一个新实例。

        判据统一走 _healthy()（status == "ok"），和 start() 一致。
        """
        if self.adopted:
            return self._healthy()
        return bool(self.proc and self.proc.poll() is None and self._healthy())

    def stop(self, force: bool = False) -> None:
        """停引擎。

        adopted=True 时默认**不动进程**：那不是我们拉起来的，杀掉会让下次启动
        重新加载 3.5GB 模型。只把引用清掉，让它作为半常驻服务继续活着。

        force=True 是「退出」用的：用户要的是真释放显存，所以不管 adopted 与否
        都终止进程。默认值 False，重启路径仍走「只解引用、不杀进程」的老行为。
        """
        if self.adopted and not force:
            print(
                f"[breeze] 引擎是复用的（PID={self.adopted_pid or '未知'}），"
                f"不随本次退出终止，只解除引用",
                flush=True,
            )
            self.adopted = False
            self.adopted_pid = None
            self.proc = None
            return

        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                self.proc.kill()
        elif force:
            # 复用的引擎没有 Popen 句柄：按 adopted_pid → 端口反查的顺序找 PID
            pid = self.adopted_pid or pid_listening_on(self.port)
            if pid:
                name = exe_name_of(pid)
                # fail-closed：查不到进程名（PID 已经消失 / tasklist 查不到）时也要放弃。
                # 以前写成 `if name and ...`，空名字会一路走到 taskkill 并打印
                # 「已强制终止引擎 PID=xxx」，看着成功其实什么也没杀。
                if not name or "breeze" not in name:
                    print(
                        f"[breeze] PID {pid}（{name or '查不到进程名，可能已退出'}）"
                        f"不像语音引擎，放弃终止",
                        flush=True,
                    )
                else:
                    try:
                        # 走 _run_console：taskkill 的输出是 GBK，用默认 UTF-8 解码
                        # 会在读取线程里抛 UnicodeDecodeError（输出全丢）
                        _run_console(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=20)
                        print(f"[breeze] 已强制终止引擎 PID={pid}", flush=True)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[breeze] 强制终止引擎失败: {exc}", flush=True)
            else:
                print(f"[breeze] 找不到监听 {self.port} 的进程，无需终止", flush=True)
        self.proc = None
        self.adopted = False
        self.adopted_pid = None

    # ------------------------------------------------------------------ 合成
    def _synth_raw(self, text: str, instruction: str) -> bytes:
        """单次请求，拿原始 WAV 字节。409（引擎忙）退避重试。"""
        body: dict = {
            "model": "breeze",
            "input": text,
            "instructions": instruction,
            "response_format": "wav",
        }
        if self.voice_id:
            body["voice"] = self.voice_id
        if self.cfg_scale:
            body["cfg_scale"] = self.cfg_scale
        if self.seed:
            body["seed"] = self.seed
        if self.max_new_tokens:
            body["max_new_tokens"] = self.max_new_tokens

        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        for attempt in range(4):
            req = urllib.request.Request(
                self._url + "/v1/audio/speech",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    time.sleep(1 * (2 ** attempt))
                    continue
                detail = exc.read().decode(errors="replace")[:200]
                if exc.code == 404 and self.voice_id:
                    raise RuntimeError(
                        f"breeze 不认识声线 {self.voice_id}（{detail}）。"
                        f"用 probe/save_voice.py 重新存一份，或清空 config 的 breeze.voice_id"
                    )
                raise RuntimeError(f"breeze 合成失败 HTTP {exc.code}: {detail}")
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"breeze 合成失败 {type(exc).__name__}: {exc}")
        raise RuntimeError("breeze 合成 409 busy 重试 3 次仍失败")

    def speak_wav(self, text: str, instruction: str | None = None) -> Path:
        """合成文本，返回 16-bit PCM WAV 路径（浏览器直接播）。

        instruction: 本句专用的语气方向，覆盖实例默认值。A 方案里由调用方
        （server.py）按当轮 emotion 传入；不传就用实例默认（config 的 instruction）。
        克隆模式下它只影响快慢和情绪，不改变音色。

        长文本会按 chunk_chars 切句分别合成，再把 PCM 缝成一条 WAV：
        单次请求越短，显存峰值越低、总耗时越短（64 字一次 45.9s，4×16 字 30s），
        但对外仍然只产出一个文件，调用方无需感知分片。
        片段之间补 chunk_gap_ms 的静音，听起来像换气/换行的停顿。

        输出的 WAV 一律重写过长度字段：引擎是流式写盘的，原始响应里 RIFF/data
        长度是 0xFFFFFFFF 占位，拿去当参考音频会让编码器读越界直接崩溃。
        """
        clean = text.strip()
        if not clean:
            raise ValueError("空文本")
        instr = instruction or self.instruction
        chunks = split_for_tts(clean, self.chunk_chars)

        with self._lock:
            self._counter += 1
            out = self.outdir / f"breeze_{self._counter:05d}.wav"

            # 短句直接一次合成，不折腾拼接（但要修头，见上）
            if len(chunks) <= 1:
                fmt, pcm, _sr, _ch, _bits = _parse_wav(self._synth_raw(clean, instr))
                out.write_bytes(_build_wav(fmt, pcm))
                return out

            fmt_ref: bytes | None = None
            silence = b""
            pcm_parts: list[bytes] = []
            for piece in chunks:
                fmt, pcm, sr, channels, bits = _parse_wav(self._synth_raw(piece, instr))
                if fmt_ref is None:
                    fmt_ref = fmt
                    block_align = max(1, channels * bits // 8)
                    silence = b"\x00" * (int(sr * self.chunk_gap_ms / 1000) * block_align)
                elif fmt != fmt_ref:
                    # 格式变了就没法直接接，宁可报错也别产出坏音频
                    raise RuntimeError("分片 WAV 格式不一致（采样率/声道/位深变了）")
                if pcm_parts:
                    pcm_parts.append(silence)
                pcm_parts.append(pcm)

            out.write_bytes(_build_wav(fmt_ref, b"".join(pcm_parts)))
            return out


if __name__ == "__main__":
    import os as _os
    root = _os.path.dirname(_os.path.abspath(__file__))
    # 和 config.json 一致的默认值
    server = r"G:\workplase\Breeze-TTS-2.cpp\breeze-tts-2-v0.1.3-windows-x64-vulkan\breeze-server.exe"
    model = r"G:\workplase\Breeze-TTS-2.cpp\breeze-tts-2-v0.1.3-windows-x64-vulkan\models\breeze-tts-2-q8_0.gguf"
    tts = Breeze(server=server, model=model, outdir=root + r"\tts_cache", port=8849)
    try:
        tts.start()
        text = sys.argv[1] if len(sys.argv) > 1 else "主人，我是小菲鱼，新引擎跑通了"
        p = tts.speak_wav(text)
        print("输出:", p, p.stat().st_size, "bytes")
    finally:
        tts.stop()
