"""把桌宠的对话接到网关（:8898）。

只保留一个出口：`ChatAgent.chat(text) -> {reply, emotion}`，和原来的接口完全一致，
所以 server.py / 前端都不用改。

网关那边一个人格、一套工具、一份记忆。桌宠和 QQ 共用这份记忆，靠说话人标签区分，
所以这里除了带 session，还要说明白是主人本人在说话。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

GATEWAY = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8898").rstrip("/")
# 和 QQ 那条线共用一个记忆槽
SESSION = os.environ.get("GATEWAY_SESSION", "main")
SPEAKER = os.environ.get("GATEWAY_SPEAKER", "主人")
TIMEOUT = float(os.environ.get("GATEWAY_TIMEOUT", "90"))

# 网关的 /chat 是桌宠专用入口，要带 token：没有它，任何本机进程发两句
# （「把 X 写进 y.md」→「好」）就能借主人的名义改文件。token 由网关启动时
# 生成在 gateway/data/gateway.token，本机进程读得到，网页读不到。
TOKEN_FILE = os.environ.get(
    "GATEWAY_TOKEN_FILE",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "gateway", "data", "gateway.token",
    ),
)


def _pet_token() -> str:
    """每次现读：网关可能是刚起来的，token 那一刻才写出来。"""
    env = os.environ.get("GATEWAY_TOKEN", "").strip()
    if env:
        return env
    try:
        with open(TOKEN_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


class ChatAgent:
    """保留旧构造参数，避免调用方改动。"""

    def __init__(self, cwd: str = "", audit_file: str | None = None,
                 approval_mode: str = "auto", max_history: int = 8):
        self.cwd = cwd
        self.approval_mode = approval_mode
        self.max_history = max_history
        self.last_error: str | None = None

    def chat(self, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            return {"reply": "", "emotion": None}

        body = json.dumps(
            {"text": text, "session": SESSION, "speaker": SPEAKER}, ensure_ascii=False
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        token = _pet_token()
        if token:
            headers["X-Gateway-Token"] = token
        req = urllib.request.Request(GATEWAY + "/chat", data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 401 = token 对不上（网关换过 token，或两边读的不是同一个文件）。
            # 别说成"没连上"，那会让人去重启一个根本没挂的服务。
            if exc.code == 401:
                self.last_error = "401 unauthorized: token 对不上"
                return {
                    "reply": "网关不认我这个身份呢，重启一下我好不好。",
                    "emotion": "confused",
                }
            # 网关连着，只是这一轮它自己出错了（500）。别说成"没连上"，
            # 那会让人去重启一个根本没挂的服务。
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:  # noqa: BLE001
                pass
            self.last_error = f"HTTP {exc.code}: {detail}"
            return {"reply": "我这边刚刚卡了一下，你再说一遍好不好。", "emotion": "confused"}
        except (urllib.error.URLError, TimeoutError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {"reply": "网关没连上呢，先把它开起来好不好。", "emotion": "sad"}
        except json.JSONDecodeError:
            self.last_error = "网关返回的不是 JSON"
            return {"reply": "网关回的东西我看不懂呀。", "emotion": "confused"}

        self.last_error = None
        return {
            "reply": payload.get("text", ""),
            "emotion": payload.get("emotion"),
            # 模型自己写的「怎么样地说」，语音那边当 instruction 用；没有就退回情绪映射
            "tone": payload.get("tone") or "",
            "sticker": payload.get("sticker"),
            "path": payload.get("path"),
        }

    def reset(self) -> None:
        """网关侧的记忆按 session 存，这里没有单独的清理接口。"""
        self.last_error = None


if __name__ == "__main__":
    import sys

    agent = ChatAgent()
    question = sys.argv[1] if len(sys.argv) > 1 else "你好呀"
    print("问:", question)
    print("答:", agent.chat(question))