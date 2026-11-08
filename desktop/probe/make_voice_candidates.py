"""生成「声线候选」：用当前 design 模式（instruction + seed）合成一段参考音频。

用途：voice design 每次按描述现编一个声音，同一句话换一句文本音色就漂。
解决办法是固定一条参考音频 -> 存成 saved voice（克隆）-> 之后都走克隆。
本脚本负责第一步：批量产出候选，挑一条满意的，再用 save_voice.py 存起来。

用法：
    python -B desktop/probe/make_voice_candidates.py            # 默认 4 个 seed
    python -B desktop/probe/make_voice_candidates.py 42 7 2026  # 指定 seed
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8849"
OUT = Path(__file__).resolve().parent / "voice_candidates"

# 参考文本：短、全是常用字、标点简单，念出来大约 8~10 秒。
# 存 saved voice 时 ref_text 必须和这里**完全一致**。
REF_TEXT = "今天风有一点凉，我把窗户关上了。你回来的时候记得跟我说一声，我给你留着灯。"
INSTRUCTION = "温柔清亮的年轻女声，语速放慢，节奏舒缓从容"
CFG_SCALE = 1.5


def synth(seed: int, text: str = REF_TEXT, instruction: str = INSTRUCTION) -> bytes:
    body = {
        "model": "breeze",
        "input": text,
        "instructions": instruction,
        "response_format": "wav",
        "seed": seed,
        "cfg_scale": CFG_SCALE,
    }
    req = urllib.request.Request(
        BASE + "/v1/audio/speech",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return resp.read()


def wav_seconds(wav: bytes) -> float:
    if len(wav) < 44:
        return 0.0
    rate = int.from_bytes(wav[24:28], "little") or 24000
    ch = int.from_bytes(wav[22:24], "little") or 1
    bits = int.from_bytes(wav[34:36], "little") or 16
    data = len(wav) - 44
    return data / (rate * ch * max(1, bits // 8))


def main() -> None:
    seeds = [int(s) for s in sys.argv[1:]] or [42, 7, 123, 2026]
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"参考文本（{len(REF_TEXT)} 字）：{REF_TEXT}")
    print(f"instruction：{INSTRUCTION}  cfg_scale={CFG_SCALE}")
    for seed in seeds:
        t0 = time.time()
        try:
            wav = synth(seed)
        except Exception as exc:  # noqa: BLE001
            print(f"[fail] seed={seed} {type(exc).__name__}: {exc}")
            continue
        path = OUT / f"seed_{seed}.wav"
        path.write_bytes(wav)
        print(f"[ok] seed={seed}  {len(wav)/1024:.0f} KB  "
              f"{wav_seconds(wav):.2f}s  耗时 {time.time()-t0:.1f}s  -> {path.name}")


if __name__ == "__main__":
    main()
