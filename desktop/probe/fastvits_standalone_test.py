"""验证：FastVITS 能否脱离 DigitalMate 独立运行。

原理：FastVITS 启动时只检查是否存在类名为 'FMTFrmManageEngine' 的窗口。
我们伪造一个同名窗口类当"宿主"，FastVITS 就会正常启动并进入消息循环。

零风险：不修改任何文件，只启动进程 + 往临时目录写一个 wav。
"""
import ctypes
import ctypes.wintypes as w
import os
import subprocess
import sys
import threading
import time

_FAST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "steam_workshop", "content", "2488350")
FASTVITS = os.path.join(_FAST, "3010132930", "FastVITS.exe")
MODEL = os.path.join(_FAST, "3010104972", "G_953000.pth")
CONFIG = os.path.join(_FAST, "3010104972", "config.json")
OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tts_probe_out")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.CreateWindowExW.restype = w.HWND
user32.CreateWindowExW.argtypes = [
    w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID,
]
user32.FindWindowW.restype = w.HWND
user32.FindWindowW.argtypes = [w.LPCWSTR, w.LPCWSTR]

WM_COPYDATA = 0x004A


class COPYDATASTRUCT(ctypes.Structure):
    _fields_ = [("dwData", ctypes.c_void_p),
                ("cbData", w.DWORD),
                ("lpData", ctypes.c_void_p)]


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


log_lines = []


def log(msg):
    print(msg, flush=True)
    log_lines.append(msg)


def main():
    os.makedirs(OUTDIR, exist_ok=True)

    # ---------- 1. 伪造 FMTFrmManageEngine 窗口 ----------
    def wndproc(hwnd, msg, wp, lp):
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    proc = WNDPROC(wndproc)          # 必须持有引用，否则被 GC
    wc = WNDCLASSW()
    wc.lpfnWndProc = proc
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.lpszClassName = "FMTFrmManageEngine"
    atom = user32.RegisterClassW(ctypes.byref(wc))
    if not atom:
        err = ctypes.get_last_error()
        # 类可能已存在（比如 DigitalMate 正在跑），直接查
        hwnd = user32.FindWindowW("FMTFrmManageEngine", None)
        if hwnd:
            log(f"[1] 复用已存在的 FMTFrmManageEngine: {hwnd:#x}")
        else:
            log(f"[1] 注册窗口类失败 err={err}")
            return 1
    else:
        hwnd = user32.CreateWindowExW(
            0, "FMTFrmManageEngine", "dm-fake-host", 0,
            0, 0, 1, 1, None, None, wc.hInstance, None,
        )
        log(f"[1] 伪造宿主窗口 OK: {hwnd:#x}")

    # 消息循环线程：FastVITS 会往这个 hwnd 发 "fastvits:<hwnd>"，不泵消息会卡住它
    stop = threading.Event()

    def pump():
        msg = w.MSG()
        while not stop.is_set():
            r = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1)
            if r:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.02)

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    log("[1] 消息循环已启动")

    # ---------- 2. 启动 FastVITS ----------
    log(f"[2] 启动 FastVITS ...")
    p = subprocess.Popen(
        [FASTVITS, str(hwnd), MODEL, CONFIG],
        cwd=os.path.dirname(FASTVITS),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    log(f"[2] pid={p.pid}，等待它注册窗口（加载 torch+cuda，可能要 30~90s）")

    # ---------- 3. 等 FastVITSWndClass 就绪 ----------
    fv_hwnd = 0
    t0 = time.time()
    while time.time() - t0 < 180:
        if p.poll() is not None:
            log(f"[3] FastVITS 提前退出，exitcode={p.returncode}")
            return 1
        fv_hwnd = user32.FindWindowW("FastVITSWndClass", "MessageReceiver")
        if fv_hwnd:
            log(f"[3] FastVITS 窗口就绪: {fv_hwnd:#x}（耗时 {time.time()-t0:.1f}s）")
            break
        time.sleep(1)
    else:
        log("[3] 超时：FastVITS 窗口未出现")
        p.terminate()
        return 1

    # ---------- 4. 发 WM_COPYDATA 合成 ----------
    text = "[ZH]主人，我是小海盐，现在可以独立说话啦[ZH]"
    speaker = 126
    payload = f"{speaker}|1|0.67|0.80|{OUTDIR}\\probe.wav|{text}"
    log(f"[4] 发送: {payload}")

    buf = ctypes.create_string_buffer(payload.encode("utf-16-le") + b"\x00\x00")
    cds = COPYDATASTRUCT()
    cds.dwData = 0
    cds.cbData = len(payload.encode("utf-16-le")) + 2
    cds.lpData = ctypes.cast(buf, ctypes.c_void_p)

    user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
    user32.SendMessageTimeoutW.argtypes = [
        w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT, ctypes.POINTER(ctypes.c_size_t)
    ]
    result = ctypes.c_size_t()
    ok = user32.SendMessageTimeoutW(
        fv_hwnd, WM_COPYDATA, hwnd, ctypes.cast(ctypes.byref(cds), w.LPARAM),
        0, 30000, ctypes.byref(result),
    )
    log(f"[4] SendMessageTimeout 返回 {ok}, result={result.value}")

    # ---------- 5. 检查 wav ----------
    wav_path = os.path.join(OUTDIR, "probe.wav")
    for _ in range(40):
        if os.path.exists(wav_path) and os.path.getsize(wav_path) > 1000:
            log(f"[5] ✅ 合成成功: {wav_path} ({os.path.getsize(wav_path):,} bytes)")
            break
        time.sleep(0.25)
    else:
        log("[5] ❌ 没有生成 wav")
        p.terminate()
        return 1

    # ---------- 6. 清理 ----------
    log("[6] 关闭 FastVITS")
    p.terminate()
    try:
        p.wait(timeout=10)
    except Exception:
        p.kill()
    stop.set()
    log("[6] 完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
