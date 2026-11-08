"""审批待办：不许覆盖，且只认挂起它的那个来源。

两件事钉死：
1. 同一会话已有未确认的待办时，第二次挂起必须失败（不覆盖）。
   以前是 delete + insert，于是主人对 A 说的「好」会落地成 B ——
   事后审计只看到「主人确认了这次写入」，内容被换却查不出来。
2. peek / take 只认 origin 相同的那条：别的地方来的请求看不到、也确认不了。

顺带钉住 `is_owner`：空 speaker 不再等于主人。
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


@pytest.fixture()
def gw(tmp_path, monkeypatch):
    import gateway

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = data_dir / "gateway.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """create table pending(
               session text primary key, action text not null,
               args text not null, ts real not null, origin text)"""
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(gateway, "DB_PATH", str(db))
    monkeypatch.setattr(gateway, "DATA_DIR", str(data_dir))
    return gateway, db


def _rows(db: str) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "select session, action, args, origin from pending"
        ).fetchall()
    finally:
        conn.close()


def test_第二次挂起不覆盖第一次(gw):
    gateway, db = gw
    assert gateway.save_pending("main", "write_file", {"path": "a.md"}, "pet") is True
    # 第二条请求想挂自己的写：必须被拒，而不是把 A 顶掉
    assert gateway.save_pending("main", "write_file", {"path": "b.md"}, "pet") is False
    rows = _rows(db)
    assert len(rows) == 1
    assert "a.md" in rows[0][2]
    assert "b.md" not in rows[0][2]


def test_确认只认挂起它的来源(gw):
    gateway, _ = gw
    gateway.save_pending("main", "write_file", {"path": "a.md"}, "qq_group:X")
    assert gateway.peek_pending("main", "pet") is None
    assert gateway.peek_pending("main", "qq_group:X") is not None


def test_别的来源取不走(gw):
    gateway, db = gw
    gateway.save_pending("main", "delete_file", {"path": "a.md"}, "qq_group:X")
    assert gateway.take_pending("main", "pet") is None
    # 没被顺手删掉：真正的来源还能确认
    assert len(_rows(db)) == 1
    assert gateway.take_pending("main", "qq_group:X") is not None
    assert _rows(db) == []


def test_过期的当作不存在(gw):
    gateway, db = gw
    gateway.save_pending("main", "write_file", {"path": "a.md"}, "pet")
    conn = sqlite3.connect(db)
    conn.execute("update pending set ts=?", (time.time() - gateway.PENDING_TTL - 1,))
    conn.commit()
    conn.close()
    assert gateway.peek_pending("main", "pet") is None
    # 过期的不算"占用"，新的一件可以挂
    assert gateway.save_pending("main", "write_file", {"path": "b.md"}, "pet") is True


def test_空speaker不再等于主人(gw):
    gateway, _ = gw
    assert gateway.is_owner("主人", False) is True
    assert gateway.is_owner("", True) is True       # 本机桌宠，已过 token 校验
    assert gateway.is_owner("", False) is False     # ← 这次修的就是这条
    assert gateway.is_owner("路人甲", False) is False
