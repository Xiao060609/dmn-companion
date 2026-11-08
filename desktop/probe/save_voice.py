"""把一条参考音频存成 saved voice（克隆），固定音色。

为什么需要它：
    desktop/config.json 里 breeze.voice_id 为空时，引擎走 **voice design**——
    按 instruction 每次现编一个声音。描述相同、seed 相同，换一句文本音色照样会漂
    （实测同一 seed 下 F0 从 250Hz 掉到 231Hz），所以听起来「每次都换一个人」。
    存一个 saved voice 后，请求带 voice_id，走 **voice clone**，音色由参考音频锁死，
    instruction 退化为「方向」（快慢、语气），不再改变是谁在说话。

用法：
    python -B desktop/probe/save_voice.py <参考wav> <名字> [--ref-text "..."] [--force]

    ref_text 必须和参考音频里念的字**完全一致**。不传就读 make_voice_candidates.REF_TEXT
    （仅当参考音频确实是用那段文本生成的）。
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE = "http://127.0.0.1:8849"
DEFAULT_REF_TEXT = "今天风有一点凉，我把窗户关上了。你回来的时候记得跟我说一声，我给你留着灯。"


def normalize_wav(raw: bytes) -> bytes:
    """把流式写出的 WAV 头补成真实长度。

    /v1/audio/speech 是边生成边写的，RIFF 和 data 两个长度字段留成 0xFFFFFFFF
    （占位）。引擎的参考音频编码器会照着这个长度去读，读越界直接段错误——
    POST /v1/voices 崩掉、breeze-cli --save-voice 崩掉都是这个原因。
    这里只改两个长度字段，不动 PCM。
    """
    if len(raw) < 44 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError("不是合法的 RIFF/WAVE")
    out = bytearray(raw)
    pos, data_off, data_len = 12, None, 0
    while pos + 8 <= len(out):
        cid = out[pos:pos + 4]
        size = int.from_bytes(out[pos + 4:pos + 8], "little")
        if cid == b"data":
            data_off = pos + 8
            data_len = len(out) - data_off
            break
        pos += 8 + size + (size & 1)
    if data_off is None:
        raise ValueError("WAV 缺少 data 段")
    out[4:8] = (data_off - 8 + data_len).to_bytes(4, "little")   # RIFF 长度
    out[data_off - 4:data_off] = data_len.to_bytes(4, "little")  # data 长度
    return bytes(out)


def _multipart(fields: dict[str, str], files: dict[str, Path]) -> tuple[bytes, str]:
    boundary = "----breeze" + uuid.uuid4().hex
    out = bytearray()
    for name, value in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += value.encode("utf-8") + b"\r\n"
    for name, item in files.items():
        if isinstance(item, tuple):
            blob, filename = item
        else:
            blob, filename = item.read_bytes(), item.name
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        out += f"--{boundary}\r\n".encode()
        out += (f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\n').encode()
        out += f"Content-Type: {ctype}\r\n\r\n".encode()
        out += blob + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def list_voices() -> list[dict]:
    with urllib.request.urlopen(BASE + "/v1/voices", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def save(path: Path, name: str, ref_text: str) -> dict:
    blob = normalize_wav(path.read_bytes())
    body, ctype = _multipart(
        {"ref_text": ref_text, "name": name,
         "description": "小菲鱼，桌宠与QQ共用的固定音色"},
        {"ref_audio": (blob, path.name)},
    )
    req = urllib.request.Request(BASE + "/v1/voices", data=body,
                                headers={"Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("name")
    ap.add_argument("--ref-text", default=DEFAULT_REF_TEXT)
    ap.add_argument("--force", action="store_true", help="同名覆盖")
    args = ap.parse_args()

    path = Path(args.wav).resolve()
    if not path.exists():
        sys.exit(f"找不到 {path}")

    exist = {v["id"]: v for v in list_voices()}
    if args.name in exist and not args.force:
        sys.exit(f"已存在同名声线 {args.name}，要覆盖加 --force")

    try:
        info = save(path, args.name, args.ref_text)
    except urllib.error.HTTPError as exc:
        sys.exit(f"保存失败 HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}")

    print("已保存:", json.dumps(info, ensure_ascii=False))
    print("\n当前声线库:")
    for v in list_voices():
        print(f"  {v['id']:<16} {v.get('seconds', 0):5.2f}s  saved={v.get('saved')}")


if __name__ == "__main__":
    main()
