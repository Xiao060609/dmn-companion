"""独立 FastVITS 宿主：不依赖 DigitalMate，音色保持原样（speakerID=126 早柚）。

原理（已实测验证）：
  FastVITS.exe 启动时只做一件事——FindWindowW("FMTFrmManageEngine") 是否存在。
  存在就继续，不存在就 sys.exit(0)。
  所以我们自己注册一个同名窗口类当"宿主"，它就会正常启动并进入消息循环。

然后通过 WM_COPYDATA 发合成请求：
  "speakerID|length_scale|noise_scale|noise_scale_w|输出wav路径|[ZH]正文[ZH]"

输出是 22050Hz / 单声道 / float32 WAV，浏览器 <audio> 不支持 float32，
所以这里顺手转成 PCM16。

设计要点：
  - 常驻：FastVITS 冷启动约 5 秒（进程 2.5s + 首次合成 2.3s，2026-10-28 实测），
    之后每句约 0.15~0.4 秒
  - 隔离：DigitalMate 在跑时，我们用自己独立的窗口类接收消息，绝不碰它的窗口
  - 无污染：DigitalMate 没跑时，临时注册 FMTFrmManageEngine 窗口类，FastVITS
    一启动完就立刻销毁并反注册，避免 DigitalMate 之后无法启动
  - 精确：多个 FastVITS 实例时，用 pid 过滤找自己的 FastVITSWndClass 窗口
  - 线程：窗口必须在消息泵线程里创建，否则 FastVITS 的阻塞 SendMessageW 会死锁
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import os
import re
import struct
import subprocess
import threading
import time
from pathlib import Path

# 必须用 use_last_error=True 的实例：ctypes.windll.user32 关掉了这个标志，
# 之后的 ctypes.get_last_error() 只会拿到 0，报错信息里的 err= 就没有意义了
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_COPYDATA = 0x004A
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
ERROR_CLASS_ALREADY_EXISTS = 1410

REQUIRED_HOST_CLASS = "FMTFrmManageEngine"   # FastVITS 硬编码检查的类名
OUR_HOST_CLASS = "DMBrowserHost"             # 我们自己用的类名（隔离用）

user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.RegisterClassW.argtypes = [ctypes.c_void_p]
user32.RegisterClassW.restype = w.ATOM
user32.UnregisterClassW.argtypes = [w.LPCWSTR, w.HINSTANCE]
user32.UnregisterClassW.restype = w.BOOL
user32.CreateWindowExW.argtypes = [
    w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID,
]
user32.CreateWindowExW.restype = w.HWND
user32.DestroyWindow.argtypes = [w.HWND]
user32.DestroyWindow.restype = w.BOOL
user32.FindWindowW.argtypes = [w.LPCWSTR, w.LPCWSTR]
user32.FindWindowW.restype = w.HWND
user32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.PostMessageW.restype = w.BOOL
user32.SendMessageTimeoutW.argtypes = [
    w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT, ctypes.POINTER(ctypes.c_size_t)
]
user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
kernel32.GetModuleHandleW.restype = w.HINSTANCE


class COPYDATASTRUCT(ctypes.Structure):
    _fields_ = [("dwData", ctypes.c_void_p),
                ("cbData", w.DWORD),
                ("lpData", ctypes.c_void_p)]


# ---- Job Object：父进程被杀（哪怕是 TerminateProcess）时，FastVITS 子进程一起收掉 ----
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectExtendedLimitInformation = 9
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", w.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", w.DWORD),
        ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
        ("PriorityClass", w.DWORD),
        ("SchedulingClass", w.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


kernel32.CreateJobObjectW.restype = w.HANDLE
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
kernel32.SetInformationJobObject.restype = w.BOOL
kernel32.AssignProcessToJobObject.restype = w.BOOL


class KillOnCloseJob:
    """把子进程放进 Job，父进程一消失操作系统就顺手杀掉它。"""

    def __init__(self):
        self.handle = kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            return
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(
            self.handle, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info))

    def assign(self, proc: subprocess.Popen) -> bool:
        if not self.handle:
            return False
        try:
            raw = proc._handle  # noqa: SLF001
            handle = ctypes.c_void_p(int(raw))
            return bool(kernel32.AssignProcessToJobObject(self.handle, handle))
        except Exception:
            return False

    def close(self):
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", w.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", w.HINSTANCE),
        ("hIcon", w.HICON),
        ("hCursor", w.HANDLE),
        ("hbrBackground", w.HBRUSH),
        ("lpszMenuName", w.LPCWSTR),
        ("lpszClassName", w.LPCWSTR),
    ]


def _enum_windows():
    rows = []

    @ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    def cb(h, _):
        pid = w.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, cls, 256)
        rows.append((h, cls.value, pid.value))
        return True

    user32.EnumWindows(cb, 0)
    return rows


def strip_emotion(text: str) -> str:
    """去掉 {happy} 之类情绪标记和 markdown，保留可读文本。"""
    t = re.sub(r"\{[a-zA-Z_]+\}", "", text)
    t = re.sub(r"[*_`#>\[\]]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def to_pcm16(src: str | Path, dst: str | Path, volume: float = 1.0) -> str:
    """float32/任意 WAV → 16-bit PCM WAV（浏览器兼容）。纯标准库。"""
    data = Path(src).read_bytes()
    if data[:4] != b"RIFF":
        raise ValueError("不是 WAV")
    pos = 12
    fmt = None
    pcm = None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        sz = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + sz]
        if cid == b"fmt ":
            fmt = body
        elif cid == b"data":
            pcm = body
        pos += 8 + sz + (sz % 2)
    if not fmt or pcm is None:
        raise ValueError("WAV 结构不完整")

    tag, ch, sr, _br, _ba, bits = struct.unpack("<HHIIHH", fmt[:16])
    if tag == 1 and bits == 16:
        Path(dst).write_bytes(data)
        return str(dst)

    if tag == 3:
        vals = struct.unpack(f"<{len(pcm)//4}f", pcm)
        ints = [int(max(-1.0, min(1.0, v * volume)) * 32767) for v in vals]
        pcm16 = struct.pack(f"<{len(ints)}h", *ints)
    elif tag == 1 and bits == 32:
        vals = struct.unpack(f"<{len(pcm)//4}i", pcm)
        ints = [max(-32768, min(32767, int(v * volume / 65536))) for v in vals]
        pcm16 = struct.pack(f"<{len(ints)}h", *ints)
    else:
        raise ValueError(f"不支持的 WAV 格式 tag={tag} bits={bits}")

    out = bytearray()
    out += b"RIFF"
    out += struct.pack("<I", 36 + len(pcm16))
    out += b"WAVEfmt "
    out += struct.pack("<IHHIIHH", 16, 1, ch, sr, sr * ch * 2, ch * 2, 16)
    out += b"data"
    out += struct.pack("<I", len(pcm16))
    out += pcm16
    Path(dst).write_bytes(bytes(out))
    return str(dst)


class FastVITS:
    """常驻 FastVITS 合成器。"""

    def __init__(self, exe: str, model: str, config: str, outdir: str,
                 speaker: int = 126, length_scale: float = 1.0,
                 noise_scale: float = 0.67, noise_scale_w: float = 0.80):
        self.exe = exe
        self.model = model
        self.config = config
        self.outdir = Path(outdir)
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.speaker = speaker
        self.length_scale = length_scale
        self.noise_scale = noise_scale
        self.noise_scale_w = noise_scale_w

        self.proc: subprocess.Popen | None = None
        self.hwnd = 0
        self._errlog = None

        # 宿主窗口状态（每个窗口一个 dict，允许同时存在永久宿主和临时启动门）
        self._host: dict = {}
        self._gate: dict = {}

        self._lock = threading.Lock()
        self._counter = 0
        self._last_error = ""
        self._job = KillOnCloseJob()

    # ------------------------------------------------------------------ 宿主窗口
    def _window_thread(self, class_name: str, ready: threading.Event, out: dict):
        """在独立线程里注册窗口类、创建窗口、跑消息循环（必须同线程）。"""

        def wndproc(hwnd, msg, wp, lp):
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

        out["wndproc"] = WNDPROC(wndproc)   # 必须持引用，否则被 GC 后崩溃
        wc = WNDCLASSW()
        wc.lpfnWndProc = out["wndproc"]
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = class_name
        out["wc"] = wc

        atom = user32.RegisterClassW(ctypes.byref(wc))
        out["atom"] = atom
        if not atom:
            err = ctypes.get_last_error()
            if err != ERROR_CLASS_ALREADY_EXISTS:
                out["err"] = f"RegisterClassW 失败 err={err}"
                ready.set()
                return
            # 本进程里类已注册过 → 复用已有类创建窗口

        hwnd = user32.CreateWindowExW(
            0, class_name, "dm-browser-host", 0,
            0, 0, 1, 1, None, None, wc.hInstance, None,
        )
        out["hwnd"] = hwnd
        ready.set()
        if not hwnd:
            out["err"] = f"CreateWindowExW 失败 err={ctypes.get_last_error()}"
            return

        msg = w.MSG()
        while True:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        # 线程收尾：销毁窗口 + 反注册类（只有自己注册成功的才需要反注册）
        user32.DestroyWindow(hwnd)
        if atom:
            user32.UnregisterClassW(class_name, wc.hInstance)

    def _open_window(self, class_name: str, timeout: float = 5.0) -> dict:
        ready = threading.Event()
        out: dict = {}
        th = threading.Thread(target=self._window_thread,
                              args=(class_name, ready, out), daemon=True)
        th.start()
        if not ready.wait(timeout):
            raise RuntimeError("宿主窗口创建超时")
        if not out.get("hwnd"):
            raise RuntimeError(out.get("err") or "宿主窗口创建失败")
        out["thread"] = th
        return out

    def _start_host(self) -> int:
        """我们自己的永久宿主窗口（类名固定 DMBrowserHost，与 DigitalMate 完全隔离）。"""
        if self._host.get("hwnd"):
            return self._host["hwnd"]
        self._host = self._open_window(OUR_HOST_CLASS)
        return self._host["hwnd"]

    def _start_gate(self, log) -> None:
        """FastVITS 启动后会一直检查「FMTFrmManageEngine 类名的窗口还在不在」，
        不在就自己退出（实测：销毁窗口后 1~2 秒 code=0 退出）。

        - DigitalMate 在跑 → 用它自己的窗口，我们什么都不用造；
        - DigitalMate 没跑 → 我们自己造一个，并且**必须一直留着**，
          直到 DigitalMate 起来（那时它的窗口就能顶上，我们再把门让出去）。
          窗口类是按进程注册的，不会妨碍 DigitalMate 之后自己启动。
        """
        if user32.FindWindowW(REQUIRED_HOST_CLASS, None):
            return
        self._gate = self._open_window(REQUIRED_HOST_CLASS)
        log(f"[tts] 启动门 {self._gate['hwnd']:#x}（类名 {REQUIRED_HOST_CLASS}）")
        threading.Thread(target=self._gate_guard, daemon=True).start()

    def _gate_guard(self) -> None:
        """DigitalMate 起来之后，把我们的门让出去，减少对它可能的干扰。"""
        while self._gate.get("hwnd"):
            time.sleep(5)
            for _h, cls, pid in _enum_windows():
                if cls == REQUIRED_HOST_CLASS and pid != os.getpid():
                    print("[tts] 检测到 DigitalMate 的宿主窗口，交还启动门", flush=True)
                    self._release_gate()
                    return

    def _close_window(self, win: dict) -> None:
        if not win or not win.get("hwnd"):
            return
        user32.PostMessageW(win["hwnd"], WM_CLOSE, 0, 0)
        th = win.get("thread")
        if th:
            th.join(timeout=3)
        win["hwnd"] = 0

    def _release_gate(self) -> None:
        self._close_window(self._gate)
        self._gate = {}

    # ------------------------------------------------------------------ 生命周期
    def start(self, timeout: float = 240.0, log=print) -> None:
        if self.proc and self.proc.poll() is None and self._find_own_window():
            self.hwnd = self._find_own_window()
            return

        host = self._start_host()
        log(f"[tts] 宿主窗口 {host:#x}（类名 {OUR_HOST_CLASS}）")

        self._start_gate(log)

        log("[tts] 启动 FastVITS（冷启动约 5 秒）…")
        t0 = time.time()
        try:
            self._errlog = open(self.outdir / "fastvits.err.log", "ab", buffering=0)
            self._errlog.write(
                f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 ===\n".encode("utf-8"))
        except OSError:
            self._errlog = None
        self.proc = subprocess.Popen(
            [self.exe, str(host), self.model, self.config],
            cwd=os.path.dirname(self.exe),
            stdout=subprocess.DEVNULL,
            stderr=self._errlog or subprocess.DEVNULL,
        )
        # 放进 Job：本进程被强杀时子进程也会一起结束，不会留下吃显存的孤儿
        self._job.assign(self.proc)
        while time.time() - t0 < timeout:
            if self.proc.poll() is not None:
                raise RuntimeError(f"FastVITS 退出，code={self.proc.returncode}")
            h = self._find_own_window()
            if h:
                self.hwnd = h
                log(f"[tts] 就绪 {h:#x}（{time.time()-t0:.1f}s）")
                return
            time.sleep(0.5)
        raise RuntimeError("FastVITS 启动超时")

    def _find_own_window(self) -> int:
        if not self.proc:
            return 0
        try:
            mine = self.proc.pid
        except Exception:
            return 0
        for h, cls, pid in _enum_windows():
            if cls == "FastVITSWndClass" and pid == mine:
                return h
        return 0

    @property
    def ready(self) -> bool:
        return bool(self.hwnd) and bool(self.proc and self.proc.poll() is None)

    # ------------------------------------------------------------------ 合成
    def synthesize_raw(self, text: str, timeout_ms: int = 60000) -> Path:
        """合成到 float32 wav，返回路径。调用前需已 start()。"""
        if not self.hwnd:
            raise RuntimeError("FastVITS 未启动")
        clean = strip_emotion(text).replace("|", "，")
        if not clean:
            raise ValueError("空文本")
        with self._lock:
            self._counter += 1
            raw = self.outdir / f"tts_{self._counter:05d}.wav"
            if raw.exists():
                raw.unlink()
            payload = (f"{self.speaker}|{self.length_scale}|{self.noise_scale}|"
                       f"{self.noise_scale_w}|{raw}|[ZH]{clean}[ZH]")
            encoded = payload.encode("utf-16-le") + b"\x00\x00"
            buf = ctypes.create_string_buffer(encoded, len(encoded))
            cds = COPYDATASTRUCT()
            cds.dwData = 0
            cds.cbData = len(encoded)
            cds.lpData = ctypes.cast(buf, ctypes.c_void_p)
            res = ctypes.c_size_t()
            ok = user32.SendMessageTimeoutW(
                self.hwnd, WM_COPYDATA, self._host.get("hwnd") or 0,
                ctypes.addressof(cds), 0, timeout_ms, ctypes.byref(res),
            )
            if not ok:
                raise RuntimeError("SendMessageTimeout 失败（插件卡住或已退出）")
            t0 = time.time()
            while time.time() - t0 < timeout_ms / 1000:
                if raw.exists() and raw.stat().st_size > 1000:
                    time.sleep(0.05)
                    return raw
                time.sleep(0.02)
            raise RuntimeError("等待 wav 超时")

    def speak_wav(self, text: str) -> Path:
        """合成并转 PCM16，返回可直接给浏览器的 wav 路径。"""
        raw = self.synthesize_raw(text)
        out = raw.with_name(raw.stem + "_pcm16.wav")
        to_pcm16(raw, out)
        try:
            raw.unlink()
        except OSError:
            pass
        return out

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
        self.proc = None
        self.hwnd = 0
        if self._errlog:
            try:
                self._errlog.close()
            except OSError:
                pass
            self._errlog = None
        self._release_gate()
        self._close_window(self._host)
        self._host = {}


if __name__ == "__main__":
    import sys

    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "steam_workshop", "content", "2488350")
    tts = FastVITS(
        exe=os.path.join(root, "3010132930", "FastVITS.exe"),
        model=os.path.join(root, "3010104972", "G_953000.pth"),
        config=os.path.join(root, "3010104972", "config.json"),
        outdir=r"<home>\AppData\Local\Temp\opencode\fastvits_probe",
    )
    try:
        tts.start()
        text = sys.argv[1] if len(sys.argv) > 1 else "主人，我是小海盐，浏览器版已经能说话啦"
        p = tts.speak_wav(text)
        print("输出:", p, os.path.getsize(p), "bytes")
    finally:
        tts.stop()
