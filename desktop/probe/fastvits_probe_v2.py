"""FastVITS 协议验证 v2（精确到 pid）+ 诊断。

发现的问题：FastVITS 窗口类名 `FastVITSWndClass` 是全局唯一的。
若 DigitalMate 的 FastVITS 已在运行，我们启动的第二个实例注册窗口会失败。
所以本脚本：
  1. 列出所有 FastVITS 相关窗口及其 pid
  2. 精确挑出我们关心的 pid 的窗口
  3. 用 WM_COPYDATA 合成，检验 wav
"""
import ctypes
import ctypes.wintypes as w
import os
import sys
import time

OUTDIR = r"<home>\AppData\Local\Temp\opencode\fastvits_probe"
WM_COPYDATA = 0x004A

user32 = ctypes.windll.user32
user32.FindWindowW.restype = w.HWND
user32.FindWindowW.argtypes = [w.LPCWSTR, w.LPCWSTR]
user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
user32.SendMessageTimeoutW.argtypes = [
    w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT, ctypes.POINTER(ctypes.c_size_t)
]


class COPYDATASTRUCT(ctypes.Structure):
    _fields_ = [("dwData", ctypes.c_void_p),
                ("cbData", w.DWORD),
                ("lpData", ctypes.c_void_p)]


def enum_fastvits_windows():
    rows = []

    @ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    def cb(h, _):
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, cls, 256)
        if "FastVITS" in cls.value or cls.value == "FMTFrmManageEngine":
            pid = w.DWORD()
            user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            t = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(h, t, 256)
            rows.append({"hwnd": h, "cls": cls.value, "title": t.value, "pid": pid.value})
        return True

    user32.EnumWindows(cb, 0)
    return rows


def synthesize(target_hwnd, text, out_path, speaker=126):
    payload = f"{speaker}|1|0.67|0.80|{out_path}|{text}"
    raw = payload.encode("utf-16-le") + b"\x00\x00"
    buf = ctypes.create_string_buffer(raw, len(raw))
    cds = COPYDATASTRUCT()
    cds.dwData = 0
    cds.cbData = len(raw)
    cds.lpData = ctypes.cast(buf, ctypes.c_void_p)
    result = ctypes.c_size_t()
    ok = user32.SendMessageTimeoutW(
        target_hwnd, WM_COPYDATA, 0,
        ctypes.addressof(cds),
        0, 30000, ctypes.byref(result),
    )
    return ok, result.value, payload


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    wins = enum_fastvits_windows()
    print("=== FastVITS / 宿主窗口 ===")
    for r in wins:
        print(f"  hwnd={r['hwnd']:#x}  cls={r['cls']:<20} pid={r['pid']:<7} title={r['title']!r}")

    targets = [r for r in wins if r["cls"] == "FastVITSWndClass"]
    if not targets:
        print("\n没有 FastVITSWndClass 窗口。")
        return 1

    # 优先选 pid 不是 DigitalMate 子进程的那个（但我们这里无法直接判断，先全试）
    out = os.path.join(OUTDIR, "probe_v2.wav")
    if os.path.exists(out):
        os.remove(out)

    for t in targets:
        print(f"\n>>> 发给 hwnd={t['hwnd']:#x} (pid={t['pid']})")
        text = "[ZH]主人，我是小海盐，现在可以独立说话啦[ZH]"
        ok, res, payload = synthesize(t["hwnd"], text, out)
        print(f"    SendMessageTimeout 返回={ok} result={res}")
        print(f"    等待 wav ...")
        for _ in range(120):
            if os.path.exists(out) and os.path.getsize(out) > 1000:
                print(f"    ✅ 生成 {out} ({os.path.getsize(out):,} bytes)")
                return 0
            time.sleep(0.25)
        print("    ❌ 未生成")
    return 1


if __name__ == "__main__":
    sys.exit(main())
