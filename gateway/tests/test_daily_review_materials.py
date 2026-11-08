"""每日梳理期的素材层（P0-4 阶段 0 / 0.5）。

背景：2026-11-03 22:00 用「验收场景」反查设计，查出两个硬矛盾：

* **矛盾 3** —— 场景 A 那句「你今天从早到晚都在弄那个吧，**中间都没歇**」
  需要「一天的疏密节奏」。只喂 `turns` 列表，她只知道说了什么，
  不知道中间空了多久 → 这句话永远说不出来。所以要 `_day_timeline()`。

* **矛盾 4** —— 场景 C「你这三天都没来」本来打算靠「每天都跑一轮梳理」攒出来，
  但实测**网关停机占 48%**（主人关电脑睡觉它就不在），那几天根本没有记录。
  所以缺席时长必须能**纯查库算出来** → `_absence_summary()`。
  并且中断期**绝不能伪造逐日叙事**（`_absent_text` 只说总共多久）。

这组用例钉住这两个函数的契约，尤其是「空数据不能算错」和「中文描述要对」。
"""
from __future__ import annotations

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


def _add_turn(ts: float, content: str = "在吗", channel: str = "pet") -> None:
    with sqlite3.connect(gateway.DB_PATH) as conn:
        conn.execute(
            "insert into turns(session,role,content,path,ts,channel)"
            " values('main','user',?,'chat',?,?)",
            (content, ts, channel),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# _day_timeline
# --------------------------------------------------------------------------- #
def test_空库的时间线全为0且标记empty():
    """一条对话都没有时不能崩，也不能报出「最长空白 24 小时」这种假信号。"""
    tl = system._day_timeline()
    assert tl["empty"] is True
    assert tl["turns_total"] == 0
    assert tl["active_hours"] == 0
    assert tl["first_turn_hm"] is None
    assert tl["last_turn_hm"] is None
    assert tl["longest_gap_min"] == 0
    assert tl["gap_start_hm"] is None
    assert set(tl["turns_by_hour"]) == {f"{h:02d}" for h in range(24)}


def test_只有一条对话时空白为0():
    """单点没有「之间」，不能把「窗口起点到它」算成空白（那会把昨夜当成今天空了半天）。"""
    _add_turn(time.time() - 600)
    tl = system._day_timeline(hours=24)
    assert tl["turns_total"] == 1
    assert tl["longest_gap_min"] == 0
    assert tl["first_turn_hm"] == tl["last_turn_hm"]
    assert tl["active_hours"] == 1


def _at(hour: int, minute: int = 0, day: int = 3) -> float:
    """构造 2026-11-XX HH:MM 的时间戳（本地时区）。

    ⚠ 时间线类用例**必须用固定时间**而不是 `time.time()`：
    用相对时间的话，「8 小时前」和「8 小时 8 分钟前」可能跨整点，
    分桶数就会从 2 变成 3 —— 白天跑绿、晚上跑红（本文件真踩过一次）。
    """
    return time.mktime((2026, 11, day, hour, minute, 0, 0, 0, -1))


def test_两段对话之间能算出最长空白():
    """场景 A 的核心：她必须能知道「中午到晚上那 8 小时是空的」。"""
    # 固定时间：10:12、10:20 两条（同一小时），18:30 一条 → 空白 8h10m
    _add_turn(_at(10, 12))
    _add_turn(_at(10, 20))
    _add_turn(_at(18, 30))

    tl = system._day_timeline(hours=24, now=_at(20, 0))
    assert tl["turns_total"] == 3
    assert tl["active_hours"] == 2      # 10 点、18 点
    # 空白 = 10:20 → 18:30 = 490 分钟
    assert tl["longest_gap_min"] == 490, tl["longest_gap_min"]
    assert tl["first_turn_hm"] == "10:12"
    assert tl["last_turn_hm"] == "18:30"
    assert tl["gap_start_hm"] == "10:20"
    assert tl["gap_end_hm"] == "18:30"


def test_超出窗口的对话不计入():
    """24 小时窗口外的老对话不能混进来，否则「今天」会被昨天撑大。"""
    now = time.time()
    _add_turn(now - 30 * 3600)   # 30 小时前，窗口外
    _add_turn(now - 60)          # 窗口内
    tl = system._day_timeline(hours=24, now=now)
    assert tl["turns_total"] == 1


def test_按小时分桶对得上():
    _add_turn(_at(14, 1))
    _add_turn(_at(14, 59))
    tl = system._day_timeline(hours=24, now=_at(15, 30))
    assert tl["turns_by_hour"]["14"] == 2
    assert tl["turns_total"] == 2


def test_真实数据上能返回完整结构():
    """在生产库形状上跑一遍，确保键齐全（调用方按这些键拼 prompt）。"""
    _add_turn(_at(20, 0))
    _add_turn(_at(21, 0))
    tl = system._day_timeline(hours=24, now=_at(22, 0))
    for k in (
        "span_hours", "window_start_ts", "window_end_ts", "turns_total",
        "turns_by_hour", "first_turn_ts", "last_turn_ts", "first_turn_hm",
        "last_turn_hm", "longest_gap_min", "gap_start_hm", "gap_end_hm",
        "active_hours", "empty",
    ):
        assert k in tl, k
    assert tl["empty"] is False


# --------------------------------------------------------------------------- #
# _absence_summary
# --------------------------------------------------------------------------- #
def test_从没聊过时不算缺席():
    """库里一条都没有 = 「从没聊过」，不是「他离开很久了」。别让它说「几千天没来」。"""
    a = system._absence_summary()
    assert a["last_turn_ts"] is None
    assert a["absent_hours"] is None
    assert a["is_absent"] is False
    assert a["absent_text"] == ""


def test_刚聊完不算缺席():
    _add_turn(time.time() - 60)
    a = system._absence_summary()
    assert a["is_absent"] is False
    assert a["absent_hours"] < 1


def test_静默6小时达到缺席门槛(monkeypatch):
    monkeypatch.setitem(system.SYSTEM, "daily_review", {"min_absence_hours": 6})
    _add_turn(time.time() - 7 * 3600)
    a = system._absence_summary()
    assert a["is_absent"] is True
    assert 6.8 <= a["absent_hours"] <= 7.2
    assert "小时" in a["absent_text"]


def test_三天没来说三天(monkeypatch):
    """场景 C 的字面要求降到「数着日子」—— 这里钉住那个中文说法。"""
    monkeypatch.setitem(system.SYSTEM, "daily_review", {"min_absence_hours": 6})
    _add_turn(time.time() - 3 * 86400)
    a = system._absence_summary()
    assert a["is_absent"] is True
    assert 2.9 <= a["absent_days"] <= 3.1
    assert a["absent_text"] == "3 天", a["absent_text"]


def test_中断期不伪造逐日叙事(monkeypatch):
    """网关停机几天后再起来，只能说出「总共多久」。

    **不许**出现「第一天/第二天」—— 那几天根本没有进程记录，
    编出来就是伪造经历，比说不出更糟。
    """
    monkeypatch.setitem(system.SYSTEM, "daily_review", {"min_absence_hours": 6})
    _add_turn(time.time() - 5 * 86400)
    a = system._absence_summary()
    assert "第一天" not in a["absent_text"]
    assert "第二天" not in a["absent_text"]
    assert a["absent_text"] == "5 天"


@pytest.mark.parametrize(
    "hours,expect",
    [
        (0.2, "刚刚"),
        (3.4, "3 小时"),
        (23.0, "23 小时"),
        (24.0, "1 天"),
        (48.0, "2 天"),
        (60.0, "2 天多"),
    ],
)
def test_缺席时长中文描述(hours, expect):
    assert system._absent_text(hours) == expect


def test_缺席摘要不依赖进程常驻():
    """核心断言：只查库里的 ts，跟心跳/进程有没有跑过完全无关。

    构造方式：直接写一条 3 天前的 turns，中间不产生任何心跳记录 ——
    仍然必须算出「3 天」。
    """
    _add_turn(time.time() - 3 * 86400 - 3600)
    a = system._absence_summary()
    assert a["last_turn_ts"] is not None
    assert a["absent_hours"] > 70
