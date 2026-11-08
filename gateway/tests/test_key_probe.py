"""钥匙串的「测试」动作：只测不改。

和「切换」的区别是**不能有副作用**——测一把废 key 不该把 active 改掉，
也不该把它删掉。测试用的是当前服务商 + 当前模型，所以换服务商后
旧 key 会测失败，这是正确行为（它确实用不了）。
"""
from __future__ import annotations

import json
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
    monkeypatch.setattr(gateway, "KEYS_PATH", str(data_dir / "keys.json"))
    monkeypatch.setattr(gateway, "AUDIT_PATH", str(data_dir / "audit.jsonl"))
    return gateway


GOOD = "sk-good-1111"
BAD = "sk-bad-2222"


def _store(gw):
    """两把 key：good 在用，bad 备用。"""
    items = [
        {"id": gw._key_id(GOOD), "label": "好钥匙", "key": GOOD, "masked": "sk-****1111"},
        {"id": gw._key_id(BAD), "label": "坏钥匙", "key": BAD, "masked": "sk-****2222"},
    ]
    gw._save_keys({"keys": items, "active": items[0]["id"]})
    return items


class TestTest:
    def test_有效key返回可用和耗时(self, gw, monkeypatch):
        items = _store(gw)
        monkeypatch.setattr(gw, "_probe_role", lambda role: (True, "", 37))

        ok, message = gw._keys_action({"action": "test", "id": items[1]["id"]})

        assert ok is True
        assert "可用" in message
        assert "37" in message

    def test_无效key返回不可用和原因(self, gw, monkeypatch):
        items = _store(gw)
        monkeypatch.setattr(gw, "_probe_role", lambda role: (False, "HTTP 401: invalid api key", 12))

        ok, message = gw._keys_action({"action": "test", "id": items[1]["id"]})

        assert ok is False
        assert "不可用" in message
        assert "401" in message

    def test_测试不会切换active(self, gw, monkeypatch):
        items = _store(gw)
        before = gw._load_keys()["active"]
        monkeypatch.setattr(gw, "_probe_role", lambda role: (True, "", 20))

        gw._keys_action({"action": "test", "id": items[1]["id"]})

        assert gw._load_keys()["active"] == before, "测试不该动 active"

    def test_测试不会删key(self, gw, monkeypatch):
        items = _store(gw)
        monkeypatch.setattr(gw, "_probe_role", lambda role: (False, "boom", 0))

        gw._keys_action({"action": "test", "id": items[1]["id"]})

        assert len(gw._load_keys()["keys"]) == 2

    def test_用的是被测试那把key(self, gw, monkeypatch):
        items = _store(gw)
        seen = {}

        def fake_probe(role):
            seen["key"] = role.get("key")
            return True, "", 5

        monkeypatch.setattr(gw, "_probe_role", fake_probe)
        gw._keys_action({"action": "test", "id": items[1]["id"]})

        assert seen["key"] == BAD, "拿错 key 去测，结果就没意义了"

    def test_不存在的id给明确错误(self, gw):
        _store(gw)
        ok, message = gw._keys_action({"action": "test", "id": "nope"})
        assert ok is False
        assert "没找到" in message


class TestProbe:
    """单独试一把还没存的 key：只连、不存、不切。"""

    def test_可用返回耗时(self, gw, monkeypatch):
        monkeypatch.setattr(gw, "_probe_role", lambda role: (True, "", 55))

        ok, message = gw._keys_action({"action": "probe", "key": "sk-new-9999"})

        assert ok is True
        assert "可用" in message
        assert "55" in message

    def test_不可用返回原因(self, gw, monkeypatch):
        monkeypatch.setattr(gw, "_probe_role", lambda role: (False, "HTTP 401: invalid", 8))

        ok, message = gw._keys_action({"action": "probe", "key": "sk-new-9999"})

        assert ok is False
        assert "不可用" in message
        assert "401" in message

    def test_空key直接拒(self, gw):
        ok, message = gw._keys_action({"action": "probe", "key": "   "})
        assert ok is False
        assert "不能为空" in message

    def test_测完不会写进钥匙串(self, gw, monkeypatch):
        monkeypatch.setattr(gw, "_probe_role", lambda role: (True, "", 10))

        gw._keys_action({"action": "probe", "key": "sk-new-9999"})

        assert gw._load_keys()["keys"] == [], "测试不该把 key 存下来"

    def test_用的是传进来那把key(self, gw, monkeypatch):
        seen = {}

        def fake(role):
            seen["key"] = role.get("key")
            return True, "", 1

        monkeypatch.setattr(gw, "_probe_role", fake)
        gw._keys_action({"action": "probe", "key": "sk-new-9999"})

        assert seen["key"] == "sk-new-9999", "测的必须是用户粘进来的那把"

    def test_已有钥匙时也不动active(self, gw, monkeypatch):
        items = _store(gw)
        before = gw._load_keys()["active"]
        monkeypatch.setattr(gw, "_probe_role", lambda role: (True, "", 9))

        gw._keys_action({"action": "probe", "key": "sk-new-9999"})

        assert gw._load_keys()["active"] == before
        assert len(gw._load_keys()["keys"]) == len(items)
