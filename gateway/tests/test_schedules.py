"""定时任务 schedules（DMN 阶段 3）的测试。

覆盖：
1. deferred_care 事件自动排一条 schedule
2. 未到点不触发（不产生内心OS、不调模型）
3. 到点触发：转成内心OS + 标记 fired
4. 已 fired 的不重复触发
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
    monkeypatch.setattr(system, "INNER_OS_PATH", str(tmp_path / "inner_os.json"))
    monkeypatch.setattr(system, "SYSTEM_AUDIT_PATH", str(tmp_path / "system_audit.jsonl"))
    import gateway
    monkeypatch.setattr(gateway, "DB_PATH", str(tmp_path / "gateway.db"))
    gateway.init_db()
    return gateway


def test_deferred_care_自动排schedule(tmp_path, monkeypatch):
    gateway = _mkdb(tmp_path, monkeypatch)
    system._save_events([{
        "kind": "deferred_care", "subject": "主人", "evidence": "说别管",
        "due_ts": time.time() + 3600, "approach_hint": "轻一点",
    }], "pet")
    with sqlite3.connect(gateway.DB_PATH) as c:
        rows = list(c.execute("select kind, event_id, note, fired from schedules"))
    assert len(rows) == 1
    assert rows[0][0] == "recheck_care"
    assert rows[0][2] == "轻一点"
    assert rows[0][3] == 0


def test_未到点不触发(tmp_path, monkeypatch):
    _mkdb(tmp_path, monkeypatch)
    system._save_events([{
        "kind": "deferred_care", "subject": "主人", "evidence": "x",
        "due_ts": time.time() + 3600,
    }], "pet")
    assert system._fire_due_schedules() == 0
    assert system._read_inner_os() == []


def test_到点触发转内心OS(tmp_path, monkeypatch):
    gateway = _mkdb(tmp_path, monkeypatch)
    system._save_events([{
        "kind": "deferred_care", "subject": "主人", "evidence": "说别管",
        "due_ts": time.time() - 1, "approach_hint": "轻一点",
    }], "pet")
    assert system._fire_due_schedules() == 1
    items = system._read_inner_os()
    assert len(items) == 1
    assert items[0]["type"] == "care_suggestion"
    assert items[0]["state"] == "pending"
    with sqlite3.connect(gateway.DB_PATH) as c:
        assert list(c.execute("select fired from schedules"))[0][0] == 1


def test_已触发不重复(tmp_path, monkeypatch):
    _mkdb(tmp_path, monkeypatch)
    system._save_events([{
        "kind": "deferred_care", "subject": "主人", "evidence": "x",
        "due_ts": time.time() - 1,
    }], "pet")
    assert system._fire_due_schedules() == 1
    assert system._fire_due_schedules() == 0  # 第二次不再触发
