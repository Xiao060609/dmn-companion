"""Breeze 引擎端到端冒烟：起 server.py → 等 /api/status tts_ready → POST /api/tts → 验 WAV。"""
import json
import struct
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8848"
WAIT_S = 90


def wait_ready():
    t0 = time.time()
    last = ""
    while time.time() - t0 < WAIT_S:
        try:
            with urllib.request.urlopen(BASE + "/api/status", timeout=2) as r:
                st = json.loads(r.read().decode())
        except Exception as e:
            last = f"未连接: {e}"
            time.sleep(1)
            continue
        if st.get("tts_ready"):
            print(f"[ok] tts ready（engine={st.get('tts_engine')}），耗时 {time.time()-t0:.1f}s")
            return st
        last = st.get("tts_error") or "loading"
        time.sleep(1)
    print(f"[fail] {WAIT_S}s 内没就绪，最后状态: {last}")
    sys.exit(1)


def post_tts(text):
    req = urllib.request.Request(
        BASE + "/api/tts",
        data=json.dumps({"text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=180) as r:
        data = r.read()
    print(f"[ok] /api/tts 返回 {len(data):,} bytes，耗时 {time.time()-t0:.1f}s，Content-Type={r.headers.get('Content-Type')}")
    return data


def check_wav(data):
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        print("[fail] 不是 WAV")
        sys.exit(1)
    pos = 12
    fmt = pcm = None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        sz = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + sz]
        if cid == b"fmt ":
            fmt = body
        elif cid == b"data":
            pcm = body
        pos += 8 + sz + (sz % 2)
    tag, ch, sr, _br, _ba, bits = struct.unpack("<HHIIHH", fmt[:16])
    secs = len(pcm) / sr / ch / (bits // 8)
    print(f"[ok] WAV: tag={tag} ch={ch} sr={sr} bits={bits} 音频 {secs:.2f}s")


if __name__ == "__main__":
    st = wait_ready()
    wav = post_tts(sys.argv[1] if len(sys.argv) > 1 else "主人，新引擎冒烟测试成功，晚安。")
    check_wav(wav)
    print("[done] 冒烟测试通过")
