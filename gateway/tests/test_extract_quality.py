"""QA 独立验证：`deferred_care` 到期时间兜底与渠道排班的不变量。

和 `test_extract_prompt.py`（工程师自测）**互不覆盖、互相独立**：那份测的是
「提示词文本写没写对」和「缺 due_hours 的那一种情况」，本文件补的是
**边界输入与夹取方向**，以及群/私聊两条渠道的完整落库语义。

覆盖：
1. `due_hours` 缺失 / None / "abc" / -5 / 1e9 五种输入，`due_ts` 都非空，
   且 `max(0.1, min(720, x))` 的夹取方向正确（-5 夹到 0.1h 而不是 720h，
   1e9 夹到 720h 而不是 0.1h）。
2. `kind=promise` 即使带 `due_hours=48` 也不产生 `due_ts`。
3. 群渠道 `deferred_care`：status=closed、due_ts 为 NULL、不排班；
   私聊/桌宠 `deferred_care`：status=deferred、due_ts 非空、排一条班
   （这条才是「迟点再修吧」会变成一次牵挂」的正向证据）。
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

import system  # noqa: E402


def _mkdb(tmp_path, monkeypatch):
    """临时库 + 建表：和 test_events.py 同一套隔离方式，绝不写生产数据。"""
    monkeypatch.setattr(system, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(system, "SYSTEM_AUDIT_PATH", str(tmp_path / "system_audit.jsonl"))
    import gateway
    monkeypatch.setattr(gateway, "DB_PATH", str(tmp_path / "gateway.db"))
    gateway.init_db()
    return gateway


# --------------------------------------------------------------------------- #
# 1. due_hours 五类输入：due_ts 必须非空，且夹取方向正确
# --------------------------------------------------------------------------- #
_MISSING = object()


@pytest.mark.parametrize(
    "raw_due_hours, expect_hours",
    [
        (_MISSING, 48.0),    # 字段整个缺失
        (None, 48.0),        # 显式 None
        ("abc", 48.0),       # 解析失败
        (-5, 0.1),           # 下界夹取
        (1e9, 720.0),        # 上界夹取
    ],
    ids=["missing", "none", "nan_str", "negative", "huge"],
)
def test_deferred_care_到期时间永不缺失且夹取方向正确(raw_due_hours, expect_hours):
    now = time.time()
    item = {"kind": "deferred_care", "subject": "主人", "evidence": "说迟点再修"}
    if raw_due_hours is not _MISSING:
        item["due_hours"] = raw_due_hours

    evs = system._validate_events({"events": [item]})
    assert len(evs) == 1, "兜底不等于丢事件"
    assert "due_ts" in evs[0], "deferred_care 缺 due_ts 就是永不排班的死数据"
    got = evs[0]["due_ts"]

    delta_h = (got - now) / 3600.0
    # 五个 case 的共同不变量：due_ts 必须非空、必须在未来
    assert got > now, f"due_ts 落在过去就是死数据：{delta_h:.3f}h"
    assert delta_h <= 721.0, f"due_ts 超出上界（{delta_h:.3f}h > 720h）"
    # 夹取方向：0.1h / 48h / 720h 三档，允许几十秒的执行抖动
    assert abs(delta_h - expect_hours) < 0.05, (
        f"夹取方向错误：期望 ~{expect_hours}h，实得 {delta_h:.3f}h")


def test_夹取方向_负值不能夹到上界():
    """-5 小时必须夹到 0.1h（尽快回头看），而不是被 min(720,-5) 误夹到 720h。"""
    now = time.time()
    evs = system._validate_events({"events": [{
        "kind": "deferred_care", "subject": "主人", "evidence": "说现在就回头看",
        "due_hours": -5,
    }]})
    delta_h = (evs[0]["due_ts"] - now) / 3600.0
    assert 0 < delta_h < 0.5, f"负值应夹到 0.1h 附近，实得 {delta_h:.3f}h"


def test_夹取方向_巨值不能夹到下界():
    """1e9 小时必须夹到 720h，而不是被 max(0.1, 1e9) 原样透传成几万年。"""
    now = time.time()
    evs = system._validate_events({"events": [{
        "kind": "deferred_care", "subject": "主人", "evidence": "说很久以后再说",
        "due_hours": 1e9,
    }]})
    delta_h = (evs[0]["due_ts"] - now) / 3600.0
    assert 700 < delta_h <= 721, f"巨值应夹到 720h，实得 {delta_h:.1f}h"


# --------------------------------------------------------------------------- #
# 2. 到期时间只属于 deferred_care
# --------------------------------------------------------------------------- #
def test_promise_带_due_hours_不产生_due_ts():
    evs = system._validate_events({"events": [{
        "kind": "promise", "subject": "主人", "evidence": "我明天弄", "due_hours": 48,
    }]})
    assert len(evs) == 1
    assert evs[0]["kind"] == "promise"
    assert "due_ts" not in evs[0]
    assert "due_hours" not in evs[0]


# --------------------------------------------------------------------------- #
# 3. 渠道语义：群不排班，私聊/桌宠真排班
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("channel", ["qq_group", "qq_group:示例群"])
def test_群渠道_deferred_care_不排班(tmp_path, monkeypatch, channel):
    gateway = _mkdb(tmp_path, monkeypatch)
    evs = system._validate_events({"events": [{
        "kind": "deferred_care", "subject": "群里某人", "evidence": "说过两天再看",
    }]})
    assert system._save_events(evs, channel) == 1
    with sqlite3.connect(gateway.DB_PATH) as c:
        status, due_ts = c.execute(
            "select status, due_ts from events where kind='deferred_care'").fetchone()
        sched = c.execute("select count(*) from schedules").fetchone()[0]
        payload = c.execute(
            "select payload from events where kind='deferred_care'").fetchone()[0]
    assert status == "closed"
    assert due_ts is None
    assert sched == 0, "群渠道排了班也没有投递能力，不许排"
    assert "group_no_delivery" in payload


@pytest.mark.parametrize("channel", ["pet", "qq_private"])
def test_主人渠道_deferred_care_真排班(tmp_path, monkeypatch, channel):
    """正向证据：「迟点再修吧」在主人渠道里会真的变成一条待办的牵挂。"""
    gateway = _mkdb(tmp_path, monkeypatch)
    evs = system._validate_events({"events": [{
        "kind": "deferred_care", "subject": "主人", "evidence": "迟点再修吧",
        "due_hours": 48, "approach_hint": "过两天问一句语音功能要不要弄，别催",
    }]})
    assert system._save_events(evs, channel) == 1
    with sqlite3.connect(gateway.DB_PATH) as c:
        status, due_ts = c.execute(
            "select status, due_ts from events where kind='deferred_care'").fetchone()
        rows = c.execute(
            "select kind, note, fired from schedules").fetchall()
    assert status == "deferred"
    assert due_ts is not None and due_ts > time.time(), "缺 due_ts = 永不回头的死数据"
    assert len(rows) == 1 and rows[0][0] == "recheck_care" and rows[0][2] == 0
    assert rows[0][1] == "过两天问一句语音功能要不要弄，别催"
