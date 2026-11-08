"""粗测 WAV 的基频（F0）中位数，纯标准库，不需要 numpy。

只为回答一个问题：同一 seed、同一 instruction，换一句文本，音色（基频）会不会漂。
帧长 40ms，自相关找基音周期，取有声帧的中位数。
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

FMIN, FMAX = 70.0, 420.0


def read_pcm(path: Path) -> tuple[list[int], int]:
    raw = path.read_bytes()
    if raw[:4] != b"RIFF":
        raise ValueError("不是 WAV")
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(raw):
        cid = raw[pos:pos + 4]
        size = int.from_bytes(raw[pos + 4:pos + 8], "little")
        if cid == b"fmt ":
            fmt = raw[pos + 8:pos + 8 + size]
        elif cid == b"data":
            data = raw[pos + 8:pos + 8 + size]
        pos += 8 + size + (size & 1)
    ch = int.from_bytes(fmt[2:4], "little")
    rate = int.from_bytes(fmt[4:8], "little")
    bits = int.from_bytes(fmt[14:16], "little")
    if bits != 16:
        raise ValueError(f"只支持 16-bit，当前 {bits}")
    step = 2 * ch
    samples = [struct.unpack_from("<h", data, i)[0] for i in range(0, len(data) - 1, step)]
    return samples, rate


def median(xs: list[float]) -> float:
    xs = sorted(xs)
    n = len(xs)
    return 0.0 if not n else (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2)


def frame_f0(frame: list[int], rate: int) -> float | None:
    energy = sum(abs(x) for x in frame) / len(frame)
    if energy < 300:  # 静音
        return None
    lo = int(rate / FMAX)
    hi = int(rate / FMIN)
    sub = frame[::2]          # 降采样到 12k，省一半算力
    r = rate // 2
    best_lag, best_score = 0, 0.0
    for lag in range(max(2, lo // 2), min(hi // 2, len(sub) // 2)):
        score = 0
        for i in range(0, len(sub) - lag, 2):
            score += sub[i] * sub[i + lag]
        if score > best_score:
            best_score, best_lag = score, lag
    return r / best_lag if best_lag else None


def f0_of(path: Path) -> tuple[float, int]:
    samples, rate = read_pcm(path)
    win = int(rate * 0.04)
    vals = []
    for start in range(0, len(samples) - win, win):
        f = frame_f0(samples[start:start + win], rate)
        if f:
            vals.append(f)
    return median(vals), len(vals)


if __name__ == "__main__":
    for p in sys.argv[1:]:
        path = Path(p)
        try:
            f0, n = f0_of(path)
            print(f"{path.name:<22} F0 中位数 {f0:6.1f} Hz  （{n} 个有声帧）")
        except Exception as exc:  # noqa: BLE001
            print(f"{path.name:<22} 失败: {exc}")
