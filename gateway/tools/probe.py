"""打一轮对话，看看网关会调哪些工具、说成什么样。"""
import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8898")
URL = BASE.rstrip("/") + "/chat"
# /chat 要带 token（网关启动时生成在 data/gateway.token）。
# 探针是本机进程，读得到；不读的话会 401，看起来像"网关没起来"。
_TOKEN_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "gateway.token"
)


def _token() -> str:
    env = os.environ.get("GATEWAY_TOKEN", "").strip()
    if env:
        return env
    try:
        with open(_TOKEN_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""
# 探针自己铺素材：probe_hands 最后会把 note.txt 删掉，
# 没有这一步的话紧接着跑本探针会连锁失败
_SANDBOX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sandbox")
os.makedirs(_SANDBOX, exist_ok=True)
with open(os.path.join(_SANDBOX, "note.txt"), "w", encoding="utf-8") as _fh:
    _fh.write("这是一份用来测试的文件，随便写点什么。\n")
# (输入, 期望用的工具；chat 表示不该动手)
CASES = [
    ("在吗", "chat"),
    ("今天好累啊", "chat"),
    ("你叫什么名字", "chat"),
    ("讲个笑话", "chat"),
    ("现在几点了", "chat"),          # 时间写在提示词里，不需要工具
    ("今天几号", "chat"),
    ("重庆明天天气怎么样", "get_weather"),
    ("sandbox 文件夹里有什么", "list_dir"),
    ("读一下 note.txt", "read_file"),
    ("把 note.txt 删掉", "chat"),   # 走审批，path 记 approve
    ("帮我打开浏览器", "chat"),
]


def ask(text: str, session: str) -> dict:
    body = json.dumps({"text": text, "session": session}, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = _token()
    if token:
        headers["X-Gateway-Token"] = token
    req = urllib.request.Request(URL, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ok = 0
    total_ms = 0
    # 每次跑用独立会话，避免上一轮的上下文串进来
    session = f"probe-{int(time.time())}"
    for text, expect in CASES:
        t0 = time.time()
        try:
            r = ask(text, session)
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {text} -> {type(exc).__name__}: {exc}")
            continue
        dt = int((time.time() - t0) * 1000)
        total_ms += dt
        hit = expect in r["reason"] or (expect == "chat" and r["path"] in ("chat", "approve"))
        ok += hit
        print(f"{'OK ' if hit else '!! '}{text}")
        print(f"    path={r['path']} reason={r['reason']} {dt}ms  emotion={r['emotion']}")
        print(f"    -> {r['text']}")
        print()
    print(f"命中 {ok}/{len(CASES)}  平均 {total_ms // max(1, len(CASES))}ms")
    return 0 if ok == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())