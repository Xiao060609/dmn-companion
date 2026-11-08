"""比较两条 WAV 的音色轮廓（长时平均频谱），纯标准库。

为什么不用基频：问句、语气上扬会让 F0 中位数漂 20~50Hz，但人耳判断「是不是同一个人」
主要靠频谱包络（共振峰）。所以这里算 24 频带的对数能量，归一化后求余弦相似度：
同一个声音 ≈ 0.95 以上，换一个人 ≈ 0.75~0.85。

用法：
    python -B desktop/probe/timbre_check.py a.wav b.wav c.wav
"""
from __future__ import annotations

import cmath
import math
import sys
from pathlib import Path

N = 1024          # 43ms @24k
BANDS = 24
FMAX = 8000.0


def read_wav(path: Path) -> tuple[list[float], int]:
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
            data = raw[pos + 8:pos + 8 + size] if size != 0xFFFFFFFF else raw[pos + 8:]
        pos += 8 + size + (size & 1)
    ch = int.from_bytes(fmt[2:4], "little")
    rate = int.from_bytes(fmt[4:8], "little")
    step = 2 * ch
    out = []
    for i in range(0, len(data) - 1, step):
        v = int.from_bytes(data[i:i + 2], "little", signed=True)
        out.append(v / 32768.0)
    return out, rate


def fft(x: list[complex]) -> list[complex]:
    n = len(x)
    j = 0
    x = x[:]
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            x[i], x[j] = x[j], x[i]
    size = 2
    while size <= n:
        half = size // 2
        w = [cmath.exp(-2j * math.pi * k / size) for k in range(half)]
        for start in range(0, n, size):
            for k in range(half):
                u = x[start + k]
                v = x[start + k + half] * w[k]
                x[start + k] = u + v
                x[start + k + half] = u - v
        size <<= 1
    return x


def spectrum(path: Path) -> list[float]:
    samples, rate = read_wav(path)
    if len(samples) / rate < 2.0:
        # 少于 2 秒的样本，长时平均频谱会被语调（问句上扬、句尾拖音）带偏，
        # 和长参考比出来的数字没有意义，只提示不拦。
        print(f"  ! {path.name} 只有 {len(samples)/rate:.1f}s，太短，相似度会偏低，别当真")
    hann = [0.5 - 0.5 * math.cos(2 * math.pi * i / (N - 1)) for i in range(N)]
    bands = [0.0] * BANDS
    frames = 0
    step = N // 2
    for start in range(0, len(samples) - N, step):
        seg = samples[start:start + N]
        if sum(abs(v) for v in seg) / N < 0.01:   # 静音跳过
            continue
        buf = [complex(seg[i] * hann[i], 0) for i in range(N)]
        mag = fft(buf)
        for b in range(BANDS):
            lo = int(N / 2 * (FMAX * b / BANDS) / (rate / 2))
            hi = int(N / 2 * (FMAX * (b + 1) / BANDS) / (rate / 2))
            hi = max(hi, lo + 1)
            e = sum(abs(mag[i]) ** 2 for i in range(lo, min(hi, N // 2)))
            bands[b] += e
        frames += 1
    if not frames:
        raise ValueError("没有有声帧")
    logs = [math.log10(b / frames + 1e-9) for b in bands]
    mean = sum(logs) / BANDS
    centered = [v - mean for v in logs]
    norm = math.sqrt(sum(v * v for v in centered)) or 1.0
    return [v / norm for v in centered]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


if __name__ == "__main__":
    paths = [Path(p) for p in sys.argv[1:]]
    specs = {}
    for p in paths:
        try:
            specs[p.name] = spectrum(p)
        except Exception as exc:  # noqa: BLE001
            print(f"{p.name}: 失败 {exc}")
    names = list(specs)
    print("音色相似度（1.00 = 完全一致）")
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            print(f"  {names[i]:<22} vs {names[j]:<22} {cosine(specs[names[i]], specs[names[j]]):.3f}")
