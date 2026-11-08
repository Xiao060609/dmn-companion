"""DMN 计划验收项的补测（阶段 3/4/5 之前漏掉的三条）。

对照 `plan\小鱼-总计划.md` 第 2.2 节（DMN 五阶段）的验收标准：

1. 阶段 3「触发时必须带最新状态重判」——定时任务转内心OS 时要附当前状态
2. 阶段 4「rejected 决定下次的 approach_hint」——被拒之后下次说话要换方式
3. 阶段 5「画像有 updated_ts，太久自动降权」——陈年画像要标注可能不准
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import gateway  # noqa: E402
import role_speak  # noqa: E402
import system  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离 DATA_DIR + 建一个干净的库。"""
    monkeypatch.setattr(system, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(system, "SYSTEM_STATE_PATH", str(tmp_path / "system_state.json"))
    monkeypatch.setattr(system, "SYSTEM_AUDIT_PATH", str(tmp_path / "system_audit.jsonl"))
    monkeypatch.setattr(system, "INNER_OS_PATH", str(tmp_path / "inner_os.json"))
    monkeypatch.setattr(system, "PEOPLE_DIR", str(tmp_path / "people"))
    db = str(tmp_path / "gateway.db")
    monkeypatch.setattr(gateway, "DB_PATH", db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "create table turns(id integer primary key autoincrement, session text, role text,"
            " content text, path text, ts real, channel text)"
        )
        conn.execute(
            "create table events(id integer primary key autoincrement, ts real, kind text,"
            " subject text, payload text, status text, due_ts real, attempts integer, channel text)"
        )
        conn.execute(
            "create table schedules(id integer primary key autoincrement, due_ts real, kind text,"
            " event_id integer, note text, fired integer default 0)"
        )
        conn.commit()
    return db


def _care(db, status, hours_ago=2.0):
    with sqlite3.connect(db) as conn:
        conn.execute(
            "insert into events(ts,kind,subject,payload,status,channel) values(?,?,?,?,?,?)",
            (time.time() - hours_ago * 3600, "care_given", "主人", "{}", status, "pet"),
        )
        conn.commit()


# ---------------------------------------------------------------- 阶段 3
def test_定时任务触发时带上当前状态(env, monkeypatch):
    """计划要求「触发时必须带最新状态重判」——不能拿两天前的判断硬执行。"""
    db = env
    # 造一个已到点的任务 + 一条最近的对话，让"当前状态"非空
    with sqlite3.connect(db) as conn:
        conn.execute(
            "insert into schedules(due_ts,kind,event_id,note,fired) values(?,?,?,?,0)",
            (time.time() - 60, "recheck_care", None, "主人之前说累"),
        )
        conn.execute(
            "insert into turns(session,role,content,path,ts,channel) values('main','user',?,?,?,?)",
            ("主人: 今天好多了", "chat", time.time() - 120, "pet"),
        )
        conn.commit()

    fired = system._fire_due_schedules()
    assert fired == 1
    items = system._read_inner_os()
    assert len(items) == 1
    ctx = items[0].get("context")
    assert isinstance(ctx, dict) and ctx, "触发时必须附当前状态"
    assert "minutes_since_last_turn" in ctx
    assert ctx["recent_chat"], "当前状态里应带上最近对话"


def test_内心OS渲染时把当前状态也带上(env):
    """状态算出来但渲染时丢掉，等于没带。"""
    block = gateway._inner_os_block([
        {"text": "该回头看看了：主人之前说累", "context": {"minutes_since_last_turn": 45.0}},
    ])
    assert "该回头看看了" in block
    assert "现在的情况" in block
    assert "45.0" in block


# ---------------------------------------------------------------- 阶段 4
def test_被拒之后下次提示换方式(env):
    db = env
    _care(db, "rejected", hours_ago=2)
    hint = system.recent_care_hint()
    assert "拒绝" in hint
    assert "别" in hint  # 要给出"别追问/别说教"这种具体约束


def test_有回应时的提示不同(env):
    db = env
    _care(db, "responded", hours_ago=5)
    hint = system.recent_care_hint()
    assert "回应" in hint
    assert "拒绝" not in hint


def test_没有关心记录时不加提示(env):
    assert system.recent_care_hint() == ""


def test_被拒的提示真的进了角色层prompt(env, monkeypatch):
    db = env
    _care(db, "rejected", hours_ago=3)
    captured = {}
    monkeypatch.setattr(gateway, "current_persona", lambda: "人设")
    monkeypatch.setattr(gateway, "CFG", {"dialog": {"max_sentences": 3}})

    def fake_call(msgs, use_tools=False):
        captured["msgs"] = msgs
        return "[不说]", {}, None

    monkeypatch.setattr(gateway, "call_model", fake_call)
    role_speak.maybe_speak({"now": "2026-11-02 22:00"}, system._default_state())
    prompt = captured["msgs"][1]["content"]
    assert "明确拒绝" in prompt


# ---------------------------------------------------------------- 阶段 5
def test_陈旧画像会标注可能不准(env, monkeypatch):
    system._write_people_profile(
        "pet",
        {
            "channel": "pet",
            "updated_ts": time.time() - 30 * 86400,
            "profile": {"称呼": "主人", "作息": "常熬夜"},
        },
    )
    captured = {}
    monkeypatch.setattr(gateway, "current_persona", lambda: "人设")
    monkeypatch.setattr(gateway, "CFG", {"dialog": {"max_sentences": 3}})

    def fake_call(msgs, use_tools=False):
        captured["msgs"] = msgs
        return "[不说]", {}, None

    monkeypatch.setattr(gateway, "call_model", fake_call)
    role_speak.maybe_speak({"now": "2026-11-02 22:00"}, system._default_state())
    prompt = captured["msgs"][1]["content"]
    assert "天前的印象" in prompt
    assert "不准" in prompt


def test_新鲜画像不加降权提示(env, monkeypatch):
    system._write_people_profile(
        "pet",
        {"channel": "pet", "updated_ts": time.time(), "profile": {"称呼": "主人"}},
    )
    captured = {}
    monkeypatch.setattr(gateway, "current_persona", lambda: "人设")
    monkeypatch.setattr(gateway, "CFG", {"dialog": {"max_sentences": 3}})

    def fake_call(msgs, use_tools=False):
        captured["msgs"] = msgs
        return "[不说]", {}, None

    monkeypatch.setattr(gateway, "call_model", fake_call)
    role_speak.maybe_speak({"now": "2026-11-02 22:00"}, system._default_state())
    prompt = captured["msgs"][1]["content"]
    assert "天前的印象" not in prompt


def test_私聊通道拒绝建模(env, monkeypatch):
    """turns 没有 speaker 列，分不清主人/陌生人——对 qq_private 建模会混淆主体，
    必须 fail-closed，而且不该为此浪费一次模型调用。"""
    called = []

    def fake_call(*a, **k):
        called.append(1)
        return None, {}, None

    monkeypatch.setattr(system, "call_system", fake_call)
    ok = system.update_people_profile("qq_private", [{"role": "user", "content": "在吗"}])
    assert ok is False
    assert called == [], "拒绝建模时不该调用模型"

