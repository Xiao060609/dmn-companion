"""结构化事件（DMN 阶段 1）的测试。

覆盖：
1. events 表能建出来（init_db）
2. _validate_events：非法 kind / 空 evidence 丢弃，deferred_care 生成 due_ts
3. _save_events 写库正确（status 区分 open/deferred）
4. 抽取失败/模型没给工具时返回空，不抛
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import system  # noqa: E402


def _mkdb(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "DATA_DIR", str(tmp_path))
    import gateway
    monkeypatch.setattr(gateway, "DB_PATH", str(tmp_path / "gateway.db"))
    gateway.init_db()
    return gateway


def test_events_表能建出来(tmp_path, monkeypatch):
    gateway = _mkdb(tmp_path, monkeypatch)
    with sqlite3.connect(gateway.DB_PATH) as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(events)")]
    assert {"id", "ts", "kind", "subject", "payload", "status", "due_ts", "attempts", "channel"} <= set(cols)


def test_validate_events_丢弃非法():
    evs = system._validate_events({"events": [
        {"kind": "emotion", "evidence": "说累"},
        {"kind": "bogus", "evidence": "x"},          # 非法 kind
        {"kind": "promise", "evidence": ""},         # 空 evidence
        {"kind": "deferred_care", "evidence": "说别管", "due_hours": 48, "approach_hint": "轻一点"},
    ]})
    assert len(evs) == 2
    assert evs[0]["kind"] == "emotion"
    assert evs[1]["kind"] == "deferred_care"
    assert "due_ts" in evs[1]
    assert evs[1]["approach_hint"] == "轻一点"


def test_save_events_只落状态机类型(tmp_path, monkeypatch):
    """2026-11-03 主从划线：`emotion`/`observation` 不再进 events。

    它们仍然会被 `_validate_events` / `_extract_events` 抽出来（人物画像要用），
    但"长期事实"的唯一库是插件的 `livingmemory.documents`，网关不再存第二份。
    """
    gateway = _mkdb(tmp_path, monkeypatch)
    n = system._save_events([
        {"kind": "emotion", "subject": "主人", "evidence": "说累"},           # 不落库
        {"kind": "observation", "subject": "主人", "evidence": "常熬夜"},      # 不落库
        {"kind": "promise", "subject": "主人", "evidence": "答应明天弄"},
        {"kind": "deferred_care", "subject": "主人", "evidence": "说别管"},
    ], "pet")
    assert n == 2, "只数真正落库的条数"
    with sqlite3.connect(gateway.DB_PATH) as c:
        rows = list(c.execute("select kind, status, channel from events order by id"))
    assert rows == [("promise", "open", "pet"), ("deferred_care", "deferred", "pet")]


def test_状态机类型可由配置覆盖(tmp_path, monkeypatch):
    """`system.events_kinds_stateful` 能改口径，不用改代码。"""
    gateway = _mkdb(tmp_path, monkeypatch)
    monkeypatch.setitem(system.SYSTEM, "events_kinds_stateful", ["observation"])
    n = system._save_events([
        {"kind": "observation", "subject": "主人", "evidence": "常熬夜"},
        {"kind": "promise", "subject": "主人", "evidence": "答应明天弄"},
    ], "pet")
    assert n == 1
    with sqlite3.connect(gateway.DB_PATH) as c:
        assert [r[0] for r in c.execute("select kind from events")] == ["observation"]


def test_配置为空时回落到默认表(tmp_path, monkeypatch):
    """坏配置不能把牵挂/承诺整条线静默关掉。"""
    _mkdb(tmp_path, monkeypatch)
    monkeypatch.setitem(system.SYSTEM, "events_kinds_stateful", [])
    assert system._stateful_event_kinds() == system.STATE_EVENT_KINDS
    monkeypatch.setitem(system.SYSTEM, "events_kinds_stateful", "不是列表")
    assert system._stateful_event_kinds() == system.STATE_EVENT_KINDS


def test_observation仍然会被抽取只是不落库():
    """降级的是**落库**，不是抽取。

    人物画像（`update_people_profile`）吃的是 `_route_turns` 过滤后的原文，
    不依赖 events；但 `_validate_events` 仍要放行 observation，否则
    "observation 还能被抽出来"这条链路会静默断掉。
    """
    evs = system._validate_events({"events": [
        {"kind": "observation", "subject": "主人", "evidence": "常熬夜到两三点"},
    ]})
    assert evs and evs[0]["kind"] == "observation"
    assert "observation" in system.EVENT_KINDS
    assert "observation" not in system._stateful_event_kinds()


def test_默认状态机类型覆盖牵挂与承诺():
    """"哪些类型要留状态"是产品决定，改动必须显式，别在重构里悄悄没了。"""
    kinds = set(system.STATE_EVENT_KINDS)
    assert {"care_rejected", "deferred_care", "promise"} <= kinds
    assert "care_given" not in kinds, "care_given 由 record_care_given 直接写，不走抽取"


def test_extract_events_模型没调工具返回空(monkeypatch):
    monkeypatch.setattr(system, "call_system", lambda msgs, **kw: (None, {}, None))
    assert system._extract_events([{"id": 1, "role": "user", "content": "x", "ts": 0}]) == []


def test_save_events_空列表不写(tmp_path, monkeypatch):
    _mkdb(tmp_path, monkeypatch)
    assert system._save_events([], "pet") == 0
