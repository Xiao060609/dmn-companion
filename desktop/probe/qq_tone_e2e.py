"""QQ 语音语气链路回归：网关输出 → AstrBot 插件清洗 → /api/tts 解析 → 合成。

为什么不直接发 HTTP 给 /api/tts：那个接口在跑着的进程里，改代码后要重启才生效。
这里把每一站的代码**原样调一遍**（插件的正则照抄 main.py，服务端用真函数），
最后落到 tts_breeze 真正合成一段音频。等重启之后，再用 --http 打真实接口。

用法：
    python -B desktop/probe/qq_tone_e2e.py            # 离线串链路 + 合成
    python -B desktop/probe/qq_tone_e2e.py --http     # 额外打真实 /api/tts
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

DESKTOP = Path(__file__).resolve().parent.parent
ROOT = DESKTOP.parent
OUT = DESKTOP / "probe" / "qq_tone"
OUT.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- 各站代码
def gateway_out(text: str, tone: str, voice: bool) -> str:
    """网关 _as_spoken 的结果。"""
    sys.path.insert(0, str(ROOT / "gateway"))
    cwd = os.getcwd()
    os.chdir(ROOT / "gateway")          # gateway.py 在导入时按 cwd 找 config
    try:
        from gateway import _as_spoken
    finally:
        os.chdir(cwd)
    return _as_spoken({"text": text, "tone": tone, "voice": voice}, "")["text"]


def plugin_clean(text: str) -> str:
    """astrbot_plugin_voice_reply 的 _clean()，正则照抄 main.py。"""
    t = text or ""
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"<[^>]{0,40}>", " ", t)
    t = re.sub(r"!?\[[^\]]{0,40}\]\([^)]{0,200}\)", " ", t)
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"[*_`#>~|]+", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


_MARKER_VARIANTS = ("[语音]", "【语音】", "(语音)", "（语音）", "[voice]", "[VOICE]")


def plugin_split_marker(text: str) -> tuple[bool, str]:
    """插件的 _split_marker()。"""
    hit = False
    out = text
    for m in _MARKER_VARIANTS:
        if m and m in out:
            hit = True
            out = out.replace(m, " ")
    return hit, out.strip()


def plugin_send_text(raw: str, max_chars: int = 100) -> str | None:
    """插件 _synthesize 前对文本做的事：判标记、清洗、截断。None = 不发语音。"""
    wants, body = plugin_split_marker(raw)
    if not wants:
        return None
    spoken = plugin_clean(body)[:max_chars]
    return spoken or None


def server_split(text: str) -> tuple[str, str]:
    """server.py 的 _split_inline_tone()，用文件里那份真代码。"""
    src = (DESKTOP / "server.py").read_text(encoding="utf-8")
    ns = {"re": re}
    exec(src[src.index("# 「怎么样地说」"):src.index("HOST = CFG.get")], ns)  # noqa: S102
    return ns["_split_inline_tone"](text)


# ---------------------------------------------------------------- 跑
CASES = [
    ("认错", "对不起对不起，我不是故意的，我这就去收拾。", "声音发虚，越说越小声"),
    ("被吓到", "晓，那个花瓶刚才掉下去了，我没拦住。", "倒吸一口气，声音发虚"),
    ("无语气", "好，我记住了。", ""),
]


def main() -> None:
    print("=" * 74)
    for name, text, tone in CASES:
        print(f"[{name}] 模型正文：{text}")
        print(f"       模型语气：{tone or '(空)'}")

        raw = gateway_out(text, tone, voice=True)
        print(f"  ① 网关出口：{raw}")

        sent = plugin_send_text(raw)
        if sent is None:
            print("  ② 插件：没识别到语音标记 → 只发文字")
            continue
        print(f"  ② 插件发出：{sent}")

        clean, got = server_split(sent)
        print(f"  ③ TTS 收到：text={clean!r} tone={got!r}")

        if "语气" in clean or "语音" in clean:
            print("  ✗ 标记漏进合成文本了")
        if got != (tone or got):
            print(f"  ✗ 语气对不上：期望 {tone!r}")
        print()

    # 真的合成一段，确认 instruction 生效（跟默认语气对比时长）
    sys.path.insert(0, str(DESKTOP))
    from tts_breeze import Breeze

    text, tone = CASES[0][1], CASES[0][2]
    raw = gateway_out(text, tone, voice=True)
    clean, got = server_split(plugin_send_text(raw) or raw)
    b = Breeze(server="x", model="y", outdir=str(OUT), port=8849, voice_id="xiaoyu",
               instruction="语速放慢，节奏舒缓从容", cfg_scale=1.5, seed=42,
               chunk_chars=28, chunk_gap_ms=250)
    for tag, instr in (("qq_tone", got or None), ("qq_flat", "语速放慢，节奏舒缓从容")):
        p = b.speak_wav(clean, instruction=instr)
        p.replace(OUT / f"{tag}.wav")
        print(f"[合成] {tag}.wav  <- {instr or '(默认)'}")
    print("=" * 74)


def http_check() -> None:
    """打真实 /api/tts（服务已重启才有意义）。"""
    body = {"text": "{语气:哽咽着说，声音发抖}[语音]对不起，我真的没看到你。"}
    req = urllib.request.Request(
        "http://127.0.0.1:8848/api/tts",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=180) as resp:
        data = resp.read()
    p = OUT / "http_inline.wav"
    p.write_bytes(data)
    print(f"[HTTP] {resp.status} {len(data)}B → {p}")


if __name__ == "__main__":
    main()
    if "--http" in sys.argv:
        http_check()
