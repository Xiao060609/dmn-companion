"""人格复盘要读得到真实对话（阶段 2'，2026-11-03 修）。

背景：`_review_persona()` 原来用 `_read_turns_since()`，那个读取器写死
`channel='pet'`。生产实况：

    pet               5 条，最后一条 2026-10-30
    qq_private      346 条（主人在用）
    qq_group:…      205 条

而水位 `last_persona_review_id` 已经是 **892**。于是
`id > 892 AND channel='pet'` 恒为空 → 审计里永远 `persona_review: no_new_turns`
→ **人格微调这条线一次都没真跑过**，而且它的失败长得像「本来就没话说」。

这组用例钉三件事：
1. 跨 pet + 主人私聊读得到增量；
2. 群聊**不进**（人格是主人的，不是群友的）；
3. 私聊里非主人的发言**不进**（`_filter_owner_turns` 仍然生效）。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import system  # noqa: E402


def _mkdb(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(system, "SYSTEM_AUDIT_PATH", str(tmp_path / "system_audit.jsonl"))
    monkeypatch.setattr(system, "SYSTEM_STATE_PATH", str(tmp_path / "system_state.json"))
    import gateway
    monkeypatch.setattr(gateway, "DB_PATH", str(tmp_path / "gateway.db"))
    gateway.init_db()
    return gateway


def _add(gateway, content: str, channel: str, role: str = "user", path: str = "chat") -> int:
    with sqlite3.connect(gateway.DB_PATH) as c:
        cur = c.execute(
            "insert into turns(session,role,content,path,ts,channel)"
            " values('main',?,?,?,?,?)",
            (role, content, path, 1793600000.0, channel),
        )
        c.commit()
        return cur.lastrowid


# --------------------------------------------------------------------------- #
# 一、读取器：读得到 / 读不到
# --------------------------------------------------------------------------- #
def test_私聊增量能被读到(tmp_path, monkeypatch):
    """这就是生产里丢的那条线：pet 没新对话，但私聊一直在聊。"""
    gateway = _mkdb(tmp_path, monkeypatch)
    tid = _add(gateway, "[私聊] 主人: 今天好累", "qq_private")
    got = system._read_persona_review_turns(0, 200)
    assert [t["id"] for t in got] == [tid]


def test_pet和私聊一起读且按id排序(tmp_path, monkeypatch):
    gateway = _mkdb(tmp_path, monkeypatch)
    a = _add(gateway, "主人: 桌宠这边", "pet")
    b = _add(gateway, "[私聊] 主人: 私聊这边", "qq_private")
    got = system._read_persona_review_turns(0, 200)
    assert [t["id"] for t in got] == sorted([a, b])


def test_水位之后的才算增量(tmp_path, monkeypatch):
    gateway = _mkdb(tmp_path, monkeypatch)
    a = _add(gateway, "[私聊] 主人: 旧的", "qq_private")
    b = _add(gateway, "[私聊] 主人: 新的", "qq_private")
    got = system._read_persona_review_turns(a, 200)
    assert [t["id"] for t in got] == [b]


def test_群聊不参与人格复盘(tmp_path, monkeypatch):
    """群里主要是别人说话，拿它塑造人格会让她越来越像群友。"""
    gateway = _mkdb(tmp_path, monkeypatch)
    _add(gateway, "[群聊·示例群] 路人: 哈哈哈哈", "qq_group:示例群")
    assert system._read_persona_review_turns(0, 200) == []


def test_陌生人私聊不参与人格复盘(tmp_path, monkeypatch):
    """qq_private 里混着陌生人，必须过 _filter_owner_turns 只留主人的。"""
    gateway = _mkdb(tmp_path, monkeypatch)
    _add(gateway, "[私聊] 陌生人: 你是谁呀", "qq_private")
    got = system._read_persona_review_turns(0, 200)
    assert got == []


def test_internal和工具往返不参与(tmp_path, monkeypatch):
    gateway = _mkdb(tmp_path, monkeypatch)
    _add(gateway, "主人: 正常一句", "pet")
    _add(gateway, "# 任务说明\n请以第一人称回顾", "internal")
    _add(gateway, "主人: 工具往返", "pet", path="tool")
    got = system._read_persona_review_turns(0, 200)
    assert len(got) == 1
    assert got[0]["content"] == "主人: 正常一句"


def test_limit是硬上限(tmp_path, monkeypatch):
    gateway = _mkdb(tmp_path, monkeypatch)
    for i in range(10):
        _add(gateway, f"[私聊] 主人: 第{i}句", "qq_private")
    assert len(system._read_persona_review_turns(0, 4)) == 4


# --------------------------------------------------------------------------- #
# 二、端到端：_review_persona 不再永远 no_new_turns
# --------------------------------------------------------------------------- #
def test_复盘不再因为pet没货而空转(tmp_path, monkeypatch):
    """生产复现：pet 没增量、私聊有增量时，旧代码会记 no_new_turns。"""
    gateway = _mkdb(tmp_path, monkeypatch)
    for i in range(9):
        _add(gateway, f"[私聊] 主人: 第{i}句话", "qq_private")

    calls = {"n": 0}

    def fake_call_system(msgs, **kw):
        calls["n"] += 1
        return None, {}, [{"function": {"name": "system_persona_review",
                                        "arguments": '{"need_change": false}'}}]

    monkeypatch.setattr(system, "call_system", fake_call_system)
    monkeypatch.setattr(system, "_read_persona", lambda: "# 小菲鱼\n" + "x" * 500)
    monkeypatch.setattr(system, "save_state", lambda st: None)

    system._review_persona()

    assert calls["n"] == 1, "应该真的调了一次模型，而不是在 no_new_turns 直接返回"
    lines = Path(system.SYSTEM_AUDIT_PATH).read_text(encoding="utf-8").splitlines()
    assert any('"persona_review"' in ln and '"no_new_turns"' not in ln for ln in lines)


def test_复盘仍然受最小条数限制(tmp_path, monkeypatch):
    """只有 2 条对话时不该去调模型（宁缺毋滥）。"""
    gateway = _mkdb(tmp_path, monkeypatch)
    _add(gateway, "[私聊] 主人: 一句", "qq_private")
    _add(gateway, "[私聊] 主人: 两句", "qq_private")

    calls = {"n": 0}

    def fake_call_system(msgs, **kw):
        calls["n"] += 1
        return None, {}, None

    monkeypatch.setattr(system, "call_system", fake_call_system)
    monkeypatch.setattr(system, "save_state", lambda st: None)

    system._review_persona()
    assert calls["n"] == 0
