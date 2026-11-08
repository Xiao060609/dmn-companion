"""守门：每日梳理期**不许污染 5 分钟心跳那一层**（P0-4 验收标准之一）。

计划 P0-4 的"明确不做"第一条：

> **不改 5 分钟心跳**：`_tick_body` / `_detect_change` / `no_change` 分支全部原样。

这条容易被违反而不自知 —— 比如"顺手"在 `no_change` 分支里加一句
"顺便检查一下该不该梳理"。那会让心跳从**零成本**变成每次 tick 都碰梳理逻辑，
而且 5 分钟一次，一天 288 次。

这组用例钉三件事：
  1. 梳理期**关着**时，`tick()` 的行为和以前完全一样（不产生任何梳理副作用）
  2. 梳理期**开着**时，`tick()` 依然不跑梳理（那是独立线程的事）
  3. `no_change` 分支仍然只写 `last_tick_ts` + 一条审计，不多写别的键
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


def _audit_rows() -> list[dict]:
    p = Path(system.SYSTEM_AUDIT_PATH)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _events() -> list[str]:
    return [str(r.get("event")) for r in _audit_rows()]


def _enable_review(monkeypatch, **over) -> None:
    cfg = {"enabled": True, "earliest_hour": 0, "day_start_hour": 6,
           "idle_after_minutes": 0, "min_absence_hours": 0,
           "daily_model_calls_cap": 5,
           "budget": {"ruminate": 1, "prospect": 1,
                      "theory_of_mind": 1, "self_narrative": 1}}
    cfg.update(over)
    monkeypatch.setitem(system.SYSTEM, "daily_review", cfg)


def test_梳理期关着时tick不产生任何梳理副作用(monkeypatch):
    """默认关闭 → 心跳跑一轮，审计里只该有 tick，不该有 daily_review_*。"""
    monkeypatch.setitem(system.SYSTEM, "daily_review", {})
    system.tick()
    events = _events()
    assert "tick" in events
    assert not [e for e in events if str(e).startswith("daily_review")], events


def test_梳理期开着时tick依然不跑梳理(monkeypatch):
    """**关键守护**：梳理是独立线程的事，心跳里一行都不许掺。

    把门槛全调成"立刻该跑"（earliest_hour=0 / idle=0 / 缺席=0），
    再调一次 tick —— 如果实现里偷偷在 tick 里跑了梳理，这里就会抓到。
    """
    _enable_review(monkeypatch)
    monkeypatch.setattr(system, "call_system",
                        lambda *a, **k: pytest.fail("tick 不许调模型跑梳理"))
    monkeypatch.setattr(system, "daily_review",
                        lambda *a, **k: pytest.fail("tick 不许调用 daily_review"))

    system.tick()
    events = _events()
    assert "tick" in events
    assert not [e for e in events if str(e).startswith("daily_review")], events


def test_no_change分支只写last_tick_ts(monkeypatch):
    """没新对话时，心跳只该更新 `last_tick_ts`，不许动梳理的键。

    这条防的是"顺手在 no_change 里也写一下梳理状态"——
    那会让「今天该不该梳理」由心跳频率决定，而不是由日窗口决定。
    """
    monkeypatch.setitem(system.SYSTEM, "daily_review", {"enabled": True})
    st = system.load_state()
    st["last_seen_turn_id"] = 10 ** 9        # 大到不可能有新对话
    # 关键：presence 也要先对齐，否则首轮会因"在场状态跃迁"而走有变化分支
    # （`_detect_change` 有两个信号，只压住 turns 那一个是不够的）
    st["last_presence_state"] = system._presence_state(system._seconds_since_last_turn())
    st["last_daily_review_day"] = "2026-01-01"
    st["last_daily_review_ts"] = 12345.0
    system.save_state(st)

    system.tick()

    after = system.load_state()
    assert after["last_tick_ts"] > 0
    # 梳理的键一个都不许被 tick 改
    assert after["last_daily_review_day"] == "2026-01-01"
    assert after["last_daily_review_ts"] == 12345.0

    rows = _audit_rows()
    tick_rows = [r for r in rows if r.get("event") == "tick"]
    assert tick_rows and tick_rows[-1]["reason"] == "no_change"


def test_梳理不影响心跳的no_change比例(monkeypatch):
    """连跑 5 次 no_change 心跳，分布不能因为开了梳理而变化。"""
    _enable_review(monkeypatch)
    st = system.load_state()
    st["last_seen_turn_id"] = 10 ** 9
    st["last_presence_state"] = system._presence_state(system._seconds_since_last_turn())
    system.save_state(st)

    for _ in range(5):
        system.tick()

    rows = [r for r in _audit_rows() if r.get("event") == "tick"]
    assert len(rows) == 5
    assert all(r.get("reason") == "no_change" for r in rows), rows


def test_梳理线程默认不起(review_off_check=None):
    """`start()` 里只有 enabled=true 才起梳理线程 —— 不给"配了没开"留半开状态。"""
    import inspect

    src = inspect.getsource(system.start)
    assert "system-daily-review" in src
    assert "_daily_review_cfg()" in src, "应按配置决定，而不是无条件起线程"


def test_梳理期开着时start会起线程(monkeypatch):
    """真起一次线程，确认它能起来且不会立刻炸。"""
    _enable_review(monkeypatch, boot_delay_s=0, check_s=999)
    monkeypatch.setattr(system, "daily_review", lambda *a, **k: {"ok": True})

    threads = system.start()
    names = [t.name for t in threads]
    assert "system-daily-review" in names, names
    assert "system-heartbeat" in names
    # 让它跑一小会儿；线程是 daemon，不 join 也不会阻塞进程退出
    time.sleep(0.3)
