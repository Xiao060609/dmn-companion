"""后台「每日梳理期」卡片的接口（system.daily_review_payload）。

为什么要这张卡：梳理期不跑的表现是「什么都没发生」——
和「主人在忙，本来就不该跑」长得一模一样。
不把闸门状态（`should_run_now` / `blocked_by`）亮出来，线上永远查不出卡在哪一条。

这组用例钉三件事：
  1. payload 形状稳定（前端按这些键渲染）
  2. `blocked_by` 能说清"为什么没跑"
  3. 不回任何密钥
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
import system  # noqa: E402


def _add_turn(ts: float) -> None:
    with sqlite3.connect(gateway.DB_PATH) as conn:
        conn.execute(
            "insert into turns(session,role,content,path,ts,channel)"
            " values('main','user','在吗','chat',?,'pet')", (ts,),
        )
        conn.commit()


def _cfg(monkeypatch, **over) -> dict:
    cfg = {
        "enabled": True, "day_start_hour": 6, "earliest_hour": 21,
        "idle_after_minutes": 120, "once_per_day": True,
        "min_absence_hours": 6, "absence_is_material": True,
        "ruminate_requires_today_turns": True,
        "daily_model_calls_cap": 5, "memory_sink_enabled": True,
        "budget": {"ruminate": 1, "prospect": 1,
                   "theory_of_mind": 1, "self_narrative": 1},
    }
    cfg.update(over)
    monkeypatch.setitem(system.SYSTEM, "daily_review", cfg)
    return cfg


REQUIRED_KEYS = (
    "enabled", "day_start_hour", "earliest_hour", "idle_after_minutes",
    "min_absence_hours", "absence_is_material", "ruminate_requires_today_turns",
    "memory_sink_enabled", "daily_model_calls_cap", "budget", "once_per_day",
    "last_day", "last_ts", "last_ago_hours",
    "should_run_now", "blocked_by", "eligible_activities", "today_turns",
    "absence_text", "longest_gap_min", "activities", "recent",
)


def test_payload形状稳定(monkeypatch):
    _cfg(monkeypatch)
    p = system.daily_review_payload()
    for k in REQUIRED_KEYS:
        assert k in p, f"前端会读 {k}，不能少"


def test_默认关闭时blocked_by说清楚(monkeypatch):
    monkeypatch.setitem(system.SYSTEM, "daily_review", {})
    p = system.daily_review_payload()
    assert p["enabled"] is False
    assert p["should_run_now"] is False
    assert p["blocked_by"] == "disabled"


def test_刚聊完时blocked_by是not_idle(monkeypatch):
    _cfg(monkeypatch, earliest_hour=0)
    _add_turn(time.time() - 60)
    p = system.daily_review_payload()
    assert p["blocked_by"] == "not_idle"


def test_没到点时blocked_by是too_early(monkeypatch):
    _cfg(monkeypatch, earliest_hour=99)     # 永远到不了
    p = system.daily_review_payload()
    assert p["blocked_by"] == "too_early"


def test_今天跑过时blocked_by是already_ran_today(monkeypatch):
    _cfg(monkeypatch, earliest_hour=0, idle_after_minutes=0)
    _add_turn(time.time() - 7200)
    st = system.load_state()
    st["last_daily_review_day"] = system._review_day_key(time.time(), 6)
    st["last_daily_review_ts"] = time.time() - 3600
    system.save_state(st)
    p = system.daily_review_payload()
    assert p["blocked_by"] == "already_ran_today"


def test_条件齐了should_run_now为真(monkeypatch):
    """安静 + 到点 + 有素材 → 可跑。

    2026-11-04 改（T-12 双判断）：加了一条「有效新颖量达阈值」的判断。
    这条测试原来只放了一条内容较短的话，新颖量不足 150 字，
    于是走的是节律那条腿（也让 should_run_now 为真，但理由不同）。
    这里显式把阈值降到 0，测的仍然是"①~④ 条件齐了就为真"这件事本身。
    """
    _cfg(monkeypatch, earliest_hour=0, idle_after_minutes=0,
         min_absence_hours=0, min_novel_chars=0)
    _add_turn(time.time() - 7200)
    p = system.daily_review_payload()
    assert p["should_run_now"] is True
    assert p["blocked_by"] == ""


def test_eligible_activities反映素材分流(monkeypatch):
    """零对话的一天：反刍不在列表里，其余三个在（矛盾 1）。"""
    _cfg(monkeypatch, min_absence_hours=6)
    _add_turn(time.time() - 3 * 86400)
    p = system.daily_review_payload()
    assert "ruminate" not in p["eligible_activities"]
    assert "self_narrative" in p["eligible_activities"]
    assert p["absence_text"] == "3 天"


def test_recent列出最近几次审计(monkeypatch):
    _cfg(monkeypatch, earliest_hour=0, idle_after_minutes=0, min_absence_hours=0)
    _add_turn(time.time() - 7200)
    monkeypatch.setattr(system, "_system_key", lambda: "k")
    monkeypatch.setattr(system, "call_system", lambda *a, **k: ('["念头"]', {}, None))
    system.daily_review()

    p = system.daily_review_payload()
    assert p["recent"], "跑过一次就该能看到记录"
    r = p["recent"][0]
    assert r["ok"] is True
    assert r["activities"]
    assert r["thoughts"] >= 1


def test_payload不含任何密钥(monkeypatch):
    _cfg(monkeypatch)
    p = system.daily_review_payload()
    blob = json.dumps(p, ensure_ascii=False)
    assert "key" not in p
    assert "sk-" not in blob
    assert "Bearer" not in blob
