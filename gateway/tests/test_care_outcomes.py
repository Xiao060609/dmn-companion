"""安慰三态（DMN 阶段 4）的测试。

覆盖：
1. record_care_given 写一条 open 事件
2. 没回应保持 open
3. 有回应 → responded
4. 有拒绝词 → rejected
5. 跨渠道：主人在 QQ 私聊回应也算回应（2026-11-03 修）
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import system  # noqa: E402


def _mkdb(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(system, "SYSTEM_AUDIT_PATH", str(tmp_path / "system_audit.jsonl"))
    import gateway
    monkeypatch.setattr(gateway, "DB_PATH", str(tmp_path / "gateway.db"))
    gateway.init_db()
    return gateway


def _add_turn(gateway, content: str, ts: float, channel: str = "pet", role: str = "user") -> None:
    with sqlite3.connect(gateway.DB_PATH) as c:
        c.execute(
            "insert into turns(session,role,content,path,ts,channel)"
            " values('main',?,?,'chat',?,?)",
            (role, content, ts, channel),
        )
        c.commit()


def _statuses(gateway):
    with sqlite3.connect(gateway.DB_PATH) as c:
        return [r[0] for r in c.execute("select status from events order by id")]


def test_record_care_given_open(tmp_path, monkeypatch):
    gateway = _mkdb(tmp_path, monkeypatch)
    system.record_care_given({"text": "你还好吗", "emotion": "care"})
    with sqlite3.connect(gateway.DB_PATH) as c:
        rows = list(c.execute("select kind, status, channel from events"))
    assert rows == [("care_given", "open", "pet")]


def test_无回应保持open(tmp_path, monkeypatch):
    gateway = _mkdb(tmp_path, monkeypatch)
    system.record_care_given({"text": "你还好吗"})
    assert system._mark_care_outcomes() == 0
    assert _statuses(gateway) == ["open"]


def test_有回应变responded(tmp_path, monkeypatch):
    gateway = _mkdb(tmp_path, monkeypatch)
    system.record_care_given({"text": "你还好吗"})
    _add_turn(gateway, "主人: 谢谢", time.time() + 1)
    assert system._mark_care_outcomes() == 1
    assert _statuses(gateway) == ["responded"]


def test_拒绝词变rejected(tmp_path, monkeypatch):
    gateway = _mkdb(tmp_path, monkeypatch)
    system.record_care_given({"text": "要不要陪你"})
    _add_turn(gateway, "主人: 别管我", time.time() + 1)
    assert system._mark_care_outcomes() == 1
    assert _statuses(gateway) == ["rejected"]


# --------------------------------------------------------------------------- #
# 跨渠道判定（2026-11-03 修）
#
# 原来 record_care_given 写死 pet，_mark_care_outcomes 又只盯事件自己的
# channel，于是主人在 QQ 私聊里回她，三态永远判不出来。生产上那 2 条
# care_given 一直停在 open 就是这条的现场证据。
# --------------------------------------------------------------------------- #
def test_主人在私聊里回应也算回应(tmp_path, monkeypatch):
    """关心投到桌宠，主人转头在 QQ 私聊回一句——这也是回应。"""
    gateway = _mkdb(tmp_path, monkeypatch)
    system.record_care_given({"text": "还不睡呀"}, channel="pet")
    _add_turn(gateway, "[私聊] 主人: 嗯，马上了", time.time() + 1, channel="qq_private")
    assert system._mark_care_outcomes() == 1
    assert _statuses(gateway) == ["responded"]


def test_私聊里的拒绝也算拒绝(tmp_path, monkeypatch):
    gateway = _mkdb(tmp_path, monkeypatch)
    system.record_care_given({"text": "还不睡呀"}, channel="pet")
    _add_turn(gateway, "[私聊] 主人: 别管我", time.time() + 1, channel="qq_private")
    assert system._mark_care_outcomes() == 1
    assert _statuses(gateway) == ["rejected"]


def test_群里别人接话不算主人回应(tmp_path, monkeypatch):
    """群聊不是「主人回应」的渠道——否则一次没人理的关心会被误判成 responded。"""
    gateway = _mkdb(tmp_path, monkeypatch)
    system.record_care_given({"text": "还不睡呀"}, channel="pet")
    _add_turn(gateway, "[群聊·某群] 路人: 哈哈", time.time() + 1, channel="qq_group:某群")
    assert system._mark_care_outcomes() == 0
    assert _statuses(gateway) == ["open"]


def test_自己说的话不算回应(tmp_path, monkeypatch):
    """只看 role='user'。她自己后续的发言不构成「主人回应」。"""
    gateway = _mkdb(tmp_path, monkeypatch)
    system.record_care_given({"text": "还不睡呀"}, channel="pet")
    _add_turn(gateway, "那我不吵你了", time.time() + 1, channel="pet", role="assistant")
    assert system._mark_care_outcomes() == 0
    assert _statuses(gateway) == ["open"]


def test_渠道显式传入会记录到事件上(tmp_path, monkeypatch):
    gateway = _mkdb(tmp_path, monkeypatch)
    system.record_care_given({"text": "在吗"}, channel="qq_private")
    with sqlite3.connect(gateway.DB_PATH) as c:
        assert c.execute("select channel from events").fetchone()[0] == "qq_private"
