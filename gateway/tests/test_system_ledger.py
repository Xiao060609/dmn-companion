# -*- coding: utf-8 -*-
"""流水账「就一本」的守卫测试（2026-11-09）。

主人原话：
    「不要给我搞五花八门的流水账出来，流水账就一本。」
    「这才能看出时间顺序。」

设计：`turns` 表是唯一的流水账，对话和 `[系统]` 行按 id 排成一条时间线。
`system_audit.jsonl` 是**诊断日志**（含 tick 这种噪声），不是流水账。
`audit.jsonl` 只记耗时，也不是。

这个测试钉三件事：
  1. system_log 确实写在 turns 里，和对话同一张表
  2. 它**不会**被当成对话内容喂回模型（否则模型以为有人说话）
  3. 该记的动作真的记了、不该记的没记
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

import migrate_channel  # noqa: E402
import system  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. 一本：系统记录和对话在同一张表
# --------------------------------------------------------------------------- #
def test_系统记录和对话在同一张表():
    """流水账只有一本 —— `turns`。不是"再开一个文件"。
    """
    import gateway

    assert system.append_system_log("[系统] 测试：这条应该进 turns")

    conn = sqlite3.connect(gateway.DB_PATH)
    try:
        row = conn.execute(
            "select channel, role, content from turns"
            " where channel=? order by id desc limit 1",
            (migrate_channel.CHANNEL_SYSTEM_LOG,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, "系统记录没写进 turns"
    ch, role, content = row
    assert ch == migrate_channel.CHANNEL_SYSTEM_LOG
    assert content.startswith("[系统]"), "流水账行必须以 [系统] 开头"
    # role 不是 user/assistant —— 它不是"谁说的话"
    assert role not in ("user", "assistant"), (
        "系统记录不能标成 user/assistant，否则会被当成有人说话"
    )


def test_时间顺序靠_id_而不是_ts():
    """同一秒内写多条时，时间戳可能相同，顺序只能靠自增 id。

    这就是"流水账就一本"的实际好处 —— 单表自增 id 天然有先后。
    """
    import gateway

    system.append_system_log("[系统] 顺序测试：第一条")
    system.append_system_log("[系统] 顺序测试：第二条")

    conn = sqlite3.connect(gateway.DB_PATH)
    try:
        rows = conn.execute(
            "select id, content from turns where channel=?"
            " order by id desc limit 2",
            (migrate_channel.CHANNEL_SYSTEM_LOG,),
        ).fetchall()
    finally:
        conn.close()

    rows.reverse()
    assert len(rows) == 2
    assert int(rows[0][0]) < int(rows[1][0]), "id 必须递增"
    assert "第一条" in rows[0][1]
    assert "第二条" in rows[1][1]


# --------------------------------------------------------------------------- #
# 2. 不会被当成对话
# --------------------------------------------------------------------------- #
def test_对话历史里读不到系统记录():
    """`recent_turns` 必须排除 system_log。

    否则模型会把「[系统] 体检：发现 1 个问题」当成主人说的话。
    """
    import gateway

    system.append_system_log("[系统] 不该出现在对话历史里")
    rows = gateway.recent_turns("main", 50, channel="私聊", hide_private=False)
    joined = "\n".join(str(r.get("content") or "") for r in rows)
    assert "不该出现在对话历史里" not in joined


def test_系统层素材里也没有系统记录():
    """`_read_recent_chat_multi` 只读 pet + 主人私聊，system_log 不该混进来。

    它走的是另一条路：`_pending_system_log()`，有独立水位，
    在巩固 payload 里叫 `system_log` 字段 —— 和 `new_turns` 分开。
    """
    system.append_system_log("[系统] 这条只在流水账里")
    turns = system._read_recent_chat_multi(50)
    joined = "\n".join(str(t.get("content") or "") for t in turns)
    assert "这条只在流水账里" not in joined


# --------------------------------------------------------------------------- #
# 3. 该记的记、不该记的不记
# --------------------------------------------------------------------------- #
def test_巩固只在真写了记忆时记一笔():
    """`memory_written=0` 的巩固不该留痕。

    一天跑几十次巩固，大部分是「没新对话」的空转。
    记它们等于把流水账变成心跳日志。
    """
    import inspect

    # 按**行**分析，不用字符串窗口 —— 这句前面隔着一大段注释，
    # 窗口截到哪儿都不可靠。
    lines = inspect.getsource(system._consolidate_channel).splitlines()
    call_at = None
    for i, ln in enumerate(lines):
        if "append_system_log(" in ln and "巩固完成" in ln:
            call_at = i
            break
    assert call_at is not None, "巩固里找不到「巩固完成」的记账调用"

    # 往上找最近的一行 if
    guard = None
    for j in range(call_at - 1, max(-1, call_at - 5), -1):
        s = lines[j].strip()
        if s.startswith("if "):
            guard = s
            break
    assert guard is not None, "「巩固完成」上面没有 if 守卫，空转也会记一笔"
    assert "memory_written" in guard, (
        "「巩固完成」的守卫应该判 memory_written > 0，实际是：%r" % guard
    )


def test_体检只在有问题时记():
    import inspect

    src = inspect.getsource(system.health_check)
    idx = src.find("[系统] 体检")
    assert idx > 0, "找不到体检记录的写法"
    before = src[max(0, idx - 300):idx]
    assert "if problems" in before, (
        "「体检」这行必须在 if problems 里 —— 全绿的体检没什么可记的"
    )


def test_心跳不记流水账():
    """tick 每 5 分钟一次、一天 288 条。它绝不能进 turns。"""
    import inspect

    src = inspect.getsource(system.tick)
    assert "append_system_log" not in src, (
        "心跳不能写流水账 —— 一天 288 条会把对话淹掉"
    )
