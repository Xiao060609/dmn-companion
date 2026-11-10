# -*- coding: utf-8 -*-
"""主动说话「何时说」的调度（2026-11-09）。

之前网关只有 `cooldown_s`（固定冷却 30 分钟）——
等于「每 30 分钟准点可以开口一次」，机械且可预测。
人的主动关心不是在整点触发的。

现在抄 AstrBot `Proactive Chat` 的算法（但**放在网关**）：
说完就随机排下一次，间隔在 `[min_interval_minutes, max_interval_minutes]`
之间均匀取。另外加了安静时段（默认 1-7 点不开口）。

这样做的意义不只是"更像人" —— 它把「何时说」的决策权从插件拿回网关，
QQ 那条路退回成纯粹的发送通道。
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import system  # noqa: E402


@pytest.fixture()
def speak_cfg(monkeypatch):
    """钉一份确定的调度配置（不然会被生产 config.json 影响）。"""
    cfg = {
        "enabled": True,
        "min_interval_minutes": 30,
        "max_interval_minutes": 120,
        "quiet_hours": "",
    }
    monkeypatch.setitem(system.SYSTEM, "proactive_speak", cfg)
    monkeypatch.setitem(system.SYSTEM, "max_speak_per_day", 6)
    monkeypatch.setitem(system.SYSTEM, "cooldown_s", 0)
    return cfg


def _state(**kw):
    st = {"last_speak_ts": 0, "speak_count_today": 0, "next_speak_ts": 0}
    st.update(kw)
    return st


# --------------------------------------------------------------------------- #
# 随机间隔
# --------------------------------------------------------------------------- #
def test_排期落在配置区间内(speak_cfg):
    st = _state()
    for _ in range(30):
        system._schedule_next_speak(st, 1000.0)
        gap = st["next_speak_gap_s"]
        assert 30 * 60 <= gap <= 120 * 60, "间隔越界：%d 秒" % gap
        assert st["next_speak_ts"] == 1000.0 + gap


def test_排期是随机的不是常数(speak_cfg):
    """固定间隔会让人发现「她准点来一句」。至少要有变化。"""
    st = _state()
    gaps = set()
    for _ in range(20):
        system._schedule_next_speak(st, 1000.0)
        gaps.add(st["next_speak_gap_s"])
    assert len(gaps) > 1, "20 次排期全都一样，说明没在区间里取随机"


def test_区间倒挂时不会崩(speak_cfg):
    """max < min 是配置错误，不能让它抛出。"""
    speak_cfg["min_interval_minutes"] = 120
    speak_cfg["max_interval_minutes"] = 30
    cfg = system._speak_window_cfg()
    assert cfg["max_interval_s"] >= cfg["min_interval_s"]


# --------------------------------------------------------------------------- #
# 安静时段
# --------------------------------------------------------------------------- #
def _ts_at_hour(h: int) -> float:
    base = datetime.now().replace(minute=0, second=0, microsecond=0)
    return base.replace(hour=h).timestamp()


@pytest.mark.parametrize("spec,hour,expect", [
    ("1-7", 3, True),     # 区间内
    ("1-7", 1, True),     # 左闭
    ("1-7", 7, False),    # 右开
    ("1-7", 0, False),
    ("1-7", 12, False),
    ("23-6", 23, True),   # 跨零点
    ("23-6", 2, True),
    ("23-6", 6, False),
    ("23-6", 12, False),
    ("", 3, False),       # 未配置 = 不安静
    ("0-0", 3, False),    # 空区间
    ("坏配置", 3, False),  # 解析失败不崩
])
def test_安静时段判断(spec, hour, expect):
    assert system._in_quiet_hours(spec, _ts_at_hour(hour)) is expect


# --------------------------------------------------------------------------- #
# 三道闸的组合
# --------------------------------------------------------------------------- #
def test_没排过就允许说(speak_cfg):
    """全新安装 / 状态被清空时不该永远不说话。"""
    assert system._allow_speak(_state()) is True


def test_没到点就不说(speak_cfg):
    now = time.time()
    st = _state(next_speak_ts=now + 600)
    assert system._allow_speak(st) is False


def test_到点就允许(speak_cfg):
    now = time.time()
    st = _state(next_speak_ts=now - 1)
    assert system._allow_speak(st) is True


def test_安静时段直接拦不说(speak_cfg, monkeypatch):
    """安静时段连模型都不该调 —— 这是省钱也省打扰。"""
    speak_cfg["quiet_hours"] = "0-24"   # 整天都安静
    assert system._allow_speak(_state()) is False


def test_额度用完就不说(speak_cfg):
    st = _state(speak_count_today=6, last_speak_ts=time.time())
    assert system._allow_speak(st) is False


def test_总开关关着就不说(speak_cfg):
    speak_cfg["enabled"] = False
    assert system._allow_speak(_state()) is False


def test_跨天会清零计数(speak_cfg):
    """昨天的计数不该压着今天。"""
    yesterday = time.time() - 86400
    st = _state(speak_count_today=6, last_speak_ts=yesterday)
    assert system._allow_speak(st) is True
    assert st["speak_count_today"] == 0


# --------------------------------------------------------------------------- #
# 状态键必须登记
# --------------------------------------------------------------------------- #
def test_新状态键在默认表里登记了():
    """`load_state()` 只保留默认表里出现过的键。

    不登记的话 `next_speak_ts` 每次读都被丢掉 —— 等于每轮心跳都以为
    "没排过"，随机间隔彻底失效。
    """
    import inspect

    src = inspect.getsource(system._default_state)
    assert '"next_speak_ts"' in src, (
        "next_speak_ts 必须在 _default_state 里登记，"
        "否则每次 load_state 都会丢"
    )
    assert '"next_speak_gap_s"' in src


def test_说完会排下一次():
    """`_tick_body` 里说完话必须调 `_schedule_next_speak`。

    不调的话 `next_speak_ts` 永远是 0，随机间隔等于没生效。
    """
    import inspect

    src = inspect.getsource(system._tick_body)
    assert "_schedule_next_speak" in src
