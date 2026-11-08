"""内心OS 待办队列（DMN 阶段 2）的测试。

覆盖：
1. push 生成 id / state=pending / 时间戳
2. consume 返回 pending 并标 delivered，**但不删除**
3. 第二次 consume 不再返回已投递的
4. 超过 expires_ts 自动 expired，不再投递
5. mark_inner_os_state 改状态
6. 封顶优先淘汰已投递的，保住 pending
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import system  # noqa: E402


def _iso(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(system, "INNER_OS_PATH", str(tmp_path / "inner_os.json"))
    monkeypatch.setattr(system, "SYSTEM_AUDIT_PATH", str(tmp_path / "system_audit.jsonl"))


def test_push_生成id和pending(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    system.push_inner_os([{"type": "insight", "text": "观点A"}])
    items = system._read_inner_os()
    assert len(items) == 1
    assert items[0]["state"] == "pending"
    assert items[0]["id"]
    assert items[0]["created_ts"] > 0


def test_consume_标delivered不清空(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    system.push_inner_os([{"type": "insight", "text": "观点A"}])
    got = system.consume_inner_os()
    assert len(got) == 1
    # 文件里还在，只是状态变了（这是与旧「读后清空」的核心区别）
    items = system._read_inner_os()
    assert len(items) == 1
    assert items[0]["state"] == "delivered"


def test_consume_第二次不再返回(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    system.push_inner_os([{"type": "insight", "text": "观点A"}])
    system.consume_inner_os()
    assert system.consume_inner_os() == []


def test_过期自动expired(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    system.push_inner_os([{"type": "insight", "text": "观点A"}])
    items = system._read_inner_os()
    items[0]["expires_ts"] = time.time() - 1  # 设成已过期
    system._write_inner_os(items)
    assert system.consume_inner_os() == []  # 过期的不投递
    assert system._read_inner_os()[0]["state"] == "expired"


def test_mark_state(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    system.push_inner_os([{"type": "persona_suggestion", "text": "建议A"}])
    item = system._read_inner_os()[0]
    assert system.mark_inner_os_state(item["id"], "acted") is True
    assert system._read_inner_os()[0]["state"] == "acted"


def test_封顶优先保住pending(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    old = [
        {
            "id": f"os_old_{i}", "type": "insight", "text": f"旧{i}",
            "state": "delivered", "created_ts": 1000 + i, "expires_ts": 9999999999,
        }
        for i in range(system.INNER_OS_MAX)
    ]
    system._write_inner_os(old)
    system.push_inner_os([{"type": "insight", "text": "新观点"}])
    items = system._read_inner_os()
    assert len(items) == system.INNER_OS_MAX
    assert any(i["text"] == "新观点" and i["state"] == "pending" for i in items)
