"""模型调用失败的分类单测。

真实现象（2026-11-01 17:12）：百炼的 API key 失效，网关每条消息都回
「我这会儿有点卡，等下再说好不好。」—— 用户以为是随机卡顿，实际是 401。

原因是 401/403 和网络超时走了同一条兜底。重试对 401 毫无意义，
而且它必须说人话，否则根本查不出来。

本文件钉住：
1. 401/403 → 直接说密钥有问题，**不重试**；
2. 其它错误（超时/空回复）→ 仍然悄悄重试一次，再失败才说「卡」。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))


@pytest.fixture()
def gw(tmp_path, monkeypatch):
    import gateway

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = data_dir / "gateway.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """create table turns(
               id integer primary key autoincrement,
               session text not null, role text not null, content text not null,
               path text, ts real not null, channel text)"""
    )
    conn.execute(
        """create table pending(
               session text primary key, action text not null,
               args text not null, ts real not null, origin text)"""
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(gateway, "DB_PATH", str(db))
    monkeypatch.setattr(gateway, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(gateway, "AUDIT_PATH", str(data_dir / "audit.jsonl"))
    return gateway


class TestAuthError:
    @pytest.mark.parametrize("error", [
        "HTTPError: HTTP Error 401: Unauthorized",
        "HTTPError: HTTP Error 403: Forbidden",
        'invalid_api_key: Incorrect API key provided',
    ])
    def test_密钥失效要说人话且不重试(self, gw, monkeypatch, error):
        calls = {"n": 0}

        def fake_call(messages, use_tools=True):
            calls["n"] += 1
            return None, {"error": error}, None

        monkeypatch.setattr(gw, "call_model", fake_call)
        result = gw.handle_chat("在吗", "s1", speaker="主人", local_pet=True)

        assert "密钥" in result["text"], "应该直接说密钥有问题"
        assert "卡" not in result["text"], "不该退化成「我卡了」"
        assert calls["n"] == 1, "401 重试没有意义，不该多打一次"


class TestTransientError:
    def test_网络抖动仍然重试一次(self, gw, monkeypatch):
        calls = {"n": 0}

        def fake_call(messages, use_tools=True):
            calls["n"] += 1
            return None, {"error": "URLError: <urlopen error timed out>"}, None

        monkeypatch.setattr(gw, "call_model", fake_call)
        result = gw.handle_chat("在吗", "s2", speaker="主人", local_pet=True)

        assert calls["n"] == 2, "偶发失败应该悄悄重试一次"
        assert "卡" in result["text"]


class TestHelper:
    def test_识别各种写法(self, gw):
        assert gw._is_auth_error("HTTP Error 401: Unauthorized")
        assert gw._is_auth_error("HTTP Error 403")
        assert gw._is_auth_error("invalid_api_key")
        assert not gw._is_auth_error("URLError: timed out")
        assert not gw._is_auth_error("")
