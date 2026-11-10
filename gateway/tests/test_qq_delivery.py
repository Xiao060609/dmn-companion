# -*- coding: utf-8 -*-
"""网关 -> QQ 的投递管道（2026-11-09）。

背景：网关的主动说话本来只能投到桌宠（`deliver_to_pet` ->
8848 `/api/say`）。桌宠没开，她就算"想说"也送不出去。

AstrBot 有 QQ 协议栈，也提供了标准接口：

    POST /api/v1/im/messages    Authorization: Bearer <key>
    {"umo": "snowluma-onebot:FriendMessage:123", "message": "要说的话"}

所以网关不自己连 QQ，只调这个接口。**这是主人定的方向：
网关推给插件，插件只当发送通道。**

这些测试不打真网络 —— 用假 opener 验证"请求长什么样、失败怎么处置"。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import role_speak  # noqa: E402


@pytest.fixture()
def qq_cfg(monkeypatch):
    """把 QQ 投递配置钉成一份确定的值。"""
    cfg = {
        "enabled": True,
        "base": "http://127.0.0.1:6185",
        "api_key": "abk_test_key",
        "umo": "snowluma-onebot:FriendMessage:10000001",
        "timeout_s": 5,
    }
    monkeypatch.setattr(role_speak, "_qq_delivery_cfg", lambda: dict(cfg))
    return cfg


class _FakeResp:
    def __init__(self, status=200, body=b'{"status":"ok"}'):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_正常投递会打对接口和请求体(qq_cfg):
    """URL、方法、鉴权头、请求体缺一不可 —— 错一个就发不出去。"""
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["body"] = json.loads(req.data.decode("utf-8"))
        seen["timeout"] = timeout
        return _FakeResp()

    with mock.patch.object(role_speak.urllib.request, "urlopen", fake_urlopen):
        ok = role_speak.deliver_to_qq({"text": "你回来啦", "emotion": "calm"})

    assert ok is True
    assert seen["url"] == "http://127.0.0.1:6185/api/v1/im/messages"
    assert seen["method"] == "POST"
    assert seen["headers"]["authorization"] == "Bearer abk_test_key"
    assert seen["body"]["umo"] == "snowluma-onebot:FriendMessage:10000001"
    assert seen["body"]["message"] == "你回来啦"


def test_调用方可以指定别的接收人(qq_cfg):
    """群聊或别的会话要能覆盖默认 umo。"""
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp()

    with mock.patch.object(role_speak.urllib.request, "urlopen", fake_urlopen):
        role_speak.deliver_to_qq(
            {"text": "群里好热闹"}, umo="snowluma-onebot:GroupMessage:999"
        )

    assert seen["body"]["umo"] == "snowluma-onebot:GroupMessage:999"


def test_没开就不发也不记审计(qq_cfg, monkeypatch):
    """`enabled=false` 时完全不动。

    为什么不记审计：心跳每 5 分钟一次，每次都记一条"没开"会刷屏。
    """
    qq_cfg["enabled"] = False
    called = []
    audited = []
    monkeypatch.setattr(
        role_speak.urllib.request, "urlopen",
        lambda *a, **k: called.append(1) or _FakeResp(),
    )
    import system
    monkeypatch.setattr(system, "_audit", lambda r: audited.append(r))

    assert role_speak.deliver_to_qq({"text": "测试"}) is False
    assert not called, "关掉后不该发请求"
    assert not audited, "关掉后不该记审计"


def test_没有key时记一条可查的失败(qq_cfg, monkeypatch):
    """没配 key 是很常见的坑（新装机器），必须能在审计里看到原因。"""
    qq_cfg["api_key"] = ""
    audited = []
    import system
    monkeypatch.setattr(system, "_audit", lambda r: audited.append(r))

    assert role_speak.deliver_to_qq({"text": "测试"}) is False
    assert audited
    assert audited[0]["reason"] == "qq_no_api_key"


def test_没有接收人时也记失败(qq_cfg, monkeypatch):
    qq_cfg["umo"] = ""
    audited = []
    import system
    monkeypatch.setattr(system, "_audit", lambda r: audited.append(r))

    assert role_speak.deliver_to_qq({"text": "测试"}) is False
    assert audited[0]["reason"] == "qq_no_umo"


def test_网络失败不抛穿只记审计(qq_cfg, monkeypatch):
    """AstrBot 没开时不能把她说话这件事搞崩。"""
    audited = []
    import system
    monkeypatch.setattr(system, "_audit", lambda r: audited.append(r))

    def boom(*a, **k):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(role_speak.urllib.request, "urlopen", boom)

    assert role_speak.deliver_to_qq({"text": "测试"}) is False
    assert audited[0]["reason"].startswith("qq_deliver_error")


def test_非200也算失败(qq_cfg, monkeypatch):
    audited = []
    import system
    monkeypatch.setattr(system, "_audit", lambda r: audited.append(r))
    monkeypatch.setattr(
        role_speak.urllib.request, "urlopen",
        lambda *a, **k: _FakeResp(status=500),
    )

    assert role_speak.deliver_to_qq({"text": "测试"}) is False
    assert audited[0]["reason"] == "qq_http_500"


def test_空文本不投递(qq_cfg, monkeypatch):
    """她输出空串时不该往 QQ 发一条空白消息。"""
    called = []
    monkeypatch.setattr(
        role_speak.urllib.request, "urlopen",
        lambda *a, **k: called.append(1) or _FakeResp(),
    )
    assert role_speak.deliver_to_qq({"text": "   "}) is False
    assert not called


# --------------------------------------------------------------------------- #
# 在 maybe_speak 里的接线
# --------------------------------------------------------------------------- #
def test_桌宠送不到才补发QQ():
    """两条路不能都说一遍 —— 人会在桌面和 QQ 上各看到一次。

    但桌宠没开时必须走 QQ，否则"她开口了"这件事就整个浪费掉。
    """
    import inspect

    src = inspect.getsource(role_speak.maybe_speak)
    assert "pet_ok = deliver_to_pet(rendered)" in src
    assert "if not pet_ok:" in src, "QQ 只该在桌宠失败时补发"
    assert "qq_ok = deliver_to_qq(rendered)" in src


def test_两边都没送到要报delivery_failed():
    """不能因为"试过 QQ"就当成说成功了。

    审计里「她开口了」和「话没送出去」必须分得开 ——
    这正是当初把 declined / delivery_failed 拆成两种状态的理由。
    """
    import inspect

    src = inspect.getsource(role_speak.maybe_speak)
    assert 'if not pet_ok and not qq_ok:' in src
    assert '"delivery_failed"' in src
