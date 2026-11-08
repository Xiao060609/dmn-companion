"""体检的「长期记忆」一项（system._long_memory_stats / health_check ⑥）。

背景：2026-11-01 17:44 起 QQ 插件 livingmemory 的 embedding 写入全部 401，
库里的素材停在当天，跨天就忘事；而系统的体检只查网关自己的 turns/inner_os/
persona/心跳/审计，**完全看不到这条独立管道**，所以坏了 1 天半没人发现，
最后是主人问「你记得我的生日吗」才暴露。

这组用例钉的就是：以后再坏，体检必须自己叫出来。
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


def _make_long_memory_db(
    path: Path,
    *,
    last_doc_days_ago: float | None,
    failed: int = 0,
    doc_text: str = "晓说他生日是 10 月 20 日，让我记着",
    key_facts: list[str] | None = None,
):
    """造一个最小可读的 livingmemory.db（只含体检要用的两张表）。"""
    import json as _json

    with sqlite3.connect(path) as conn:
        conn.execute(
            "create table documents(id integer primary key, doc_id text, text text,"
            " metadata text, created_at datetime, updated_at datetime)"
        )
        conn.execute(
            "create table memory_write_ops(id integer primary key, op_type text,"
            " memory_id integer, status text, step text, payload text, error text,"
            " retry_count integer, created_at real, updated_at real)"
        )
        if last_doc_days_ago is not None:
            ts = time.time() - last_doc_days_ago * 86400
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            meta = _json.dumps(
                {
                    "summary": doc_text,
                    "key_facts": key_facts if key_facts is not None else ["生日 10-20"],
                },
                ensure_ascii=False,
            )
            conn.execute(
                "insert into documents(doc_id,text,metadata,created_at,updated_at)"
                " values('d1',?,?,?,?)",
                (doc_text, meta, stamp, stamp),
            )
        for i in range(failed):
            conn.execute(
                "insert into memory_write_ops(op_type,status,step,error,retry_count,"
                " created_at,updated_at) values('add','failed','document_failed',?,1,?,?)",
                ("Error code: 401 - Incorrect API key provided.", time.time() - 60, time.time() - 60),
            )
        conn.commit()


def _add_recent_turn() -> None:
    with sqlite3.connect(gateway.DB_PATH) as conn:
        conn.execute(
            "insert into turns(session,role,content,path,ts,channel)"
            " values('main','user','还在吗','chat',?,'pet')",
            (time.time(),),
        )
        conn.commit()


@pytest.fixture()
def use_lm(tmp_path, monkeypatch):
    def _install(**kw):
        db = tmp_path / "livingmemory.db"
        _make_long_memory_db(db, **kw)
        monkeypatch.setitem(system.SYSTEM, "long_memory_db", str(db))
        return db

    return _install


def _areas(report: dict) -> list[str]:
    return [p["area"] for p in report["problems"]]


def test_写入失败会被体检叫出来(use_lm):
    use_lm(last_doc_days_ago=0, failed=3)
    report = system.health_check()
    assert "long_memory" in _areas(report)
    msg = [p["msg"] for p in report["problems"] if p["area"] == "long_memory"][0]
    assert "3 次写入失败" in msg
    assert "401" in msg
    assert report["stats"]["long_memory_failed_24h"] == 3


def test_还在聊天但记忆很久没新增会报警(use_lm):
    use_lm(last_doc_days_ago=5, failed=0)
    _add_recent_turn()
    report = system.health_check()
    assert "long_memory" in _areas(report)
    msg = [p["msg"] for p in report["problems"] if p["area"] == "long_memory"][0]
    assert "没有新增" in msg
    assert "5 天" in msg


def test_人不在时不报记忆陈旧(use_lm):
    """没人在聊天，本来就不该产生新记忆——天天报等于没有报警。"""
    use_lm(last_doc_days_ago=30, failed=0)
    report = system.health_check()
    assert "long_memory" not in _areas(report)


def test_插件没装时不报错(monkeypatch, tmp_path):
    """库文件根本不存在 = 「没装」，这才是唯一合法的静默跳过。"""
    monkeypatch.setitem(
        system.SYSTEM, "long_memory_db", str(tmp_path / "not_there" / "livingmemory.db")
    )
    report = system.health_check()
    assert "long_memory" not in _areas(report)
    assert report["stats"]["long_memory_docs"] is None


def test_库存在但documents缺失必须报error(monkeypatch, tmp_path):
    """2026-11-03 22:05 改（P0-3）：**库在、但读不出来，不许静默跳过。**

    旧行为是 `except sqlite3.Error: return None`，于是「库坏了」和「库没装」
    长得一模一样 —— 一个永远不报警的健康检查比没有更危险。
    现在：documents 读不出来 → `unreadable=True` + error 级问题。
    """
    db = tmp_path / "broken.db"
    with sqlite3.connect(db) as conn:
        conn.execute("create table something_else(id integer)")
        conn.commit()
    monkeypatch.setitem(system.SYSTEM, "long_memory_db", str(db))
    report = system.health_check()
    assert "long_memory" in _areas(report)
    lv = [p["level"] for p in report["problems"] if p["area"] == "long_memory"]
    assert "error" in lv, lv
    msg = [p["msg"] for p in report["problems"] if p["area"] == "long_memory"][0]
    assert "读不出来" in msg
    # 不再返回 None
    stats = system._long_memory_stats()
    assert stats is not None
    assert stats["unreadable"] is True
    assert report["ok"] is False


# --------------------------------------------------------------------------- #
# 缺 memory_write_ops 表（2026-11-03 22:05 补，P0-3 的正面用例）
#
# 生产实况：自建库 `gateway/data/livingmemory.db` 只有 documents / documents_fts，
# **没有 memory_write_ops**。旧代码那条查询一抛，被外层 except 整段吞掉，
# 于是 long_memory_docs / failed_24h / hollow_recent **全是 None**，
# health_check 第 ⑥ 项整个跳过 —— 体检对长期记忆彻底失明。
#
# 自建库没有这张表是**正常**的（写入同步，失败直接抛调用方），
# 所以只降级这一项，其余指标必须照常算出来。
# --------------------------------------------------------------------------- #
def _make_db_without_write_ops(path: Path, *, doc_text: str = "晓说他生日是 10 月 20 日",
                               key_facts: list[str] | None = None) -> None:
    import json as _json

    with sqlite3.connect(path) as conn:
        conn.execute(
            "create table documents(id integer primary key, doc_id text, text text,"
            " metadata text, created_at datetime, updated_at datetime)"
        )
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3600))
        meta = _json.dumps(
            {"summary": doc_text,
             "key_facts": key_facts if key_facts is not None else ["生日 10-20"]},
            ensure_ascii=False,
        )
        conn.execute(
            "insert into documents(doc_id,text,metadata,created_at,updated_at)"
            " values('d1',?,?,?,?)",
            (doc_text, meta, stamp, stamp),
        )
        conn.commit()


def test_缺memory_write_ops表仍能算出其余指标(monkeypatch, tmp_path):
    """这是生产库的真实形状 —— 必须返回统计，而不是 None。"""
    db = tmp_path / "selfhosted.db"
    _make_db_without_write_ops(db)
    monkeypatch.setitem(system.SYSTEM, "long_memory_db", str(db))

    stats = system._long_memory_stats()
    assert stats is not None, "库在就不能返回 None，否则体检第 ⑥ 项彻底静默"
    assert stats["documents"] == 1          # ← P0-3 的核心：documents 必须是真值
    assert stats["failed_24h"] == 0
    assert stats["write_ops_missing"] is True
    assert stats["unreadable"] is False
    assert stats["recent_checked"] == 1
    assert stats["hollow_recent"] == 0
    assert stats["last_doc_ts"] is not None


def test_缺表时体检照样报出documents条数(monkeypatch, tmp_path):
    """判定标准：`stats["long_memory_docs"]` 要等于 documents 的真实条数。"""
    db = tmp_path / "selfhosted.db"
    _make_db_without_write_ops(db)
    monkeypatch.setitem(system.SYSTEM, "long_memory_db", str(db))

    report = system.health_check()
    assert report["stats"]["long_memory_docs"] == 1
    assert report["stats"]["long_memory_failed_24h"] == 0
    assert report["stats"]["long_memory_hollow_recent"] == 0
    # 缺表要提一句（配置态），但不是 error
    msgs = [p for p in report["problems"] if p["area"] == "long_memory"]
    assert len(msgs) == 1
    assert msgs[0]["level"] == "warn"
    assert "memory_write_ops" in msgs[0]["msg"]


def test_缺表时空壳检测仍然有效(monkeypatch, tmp_path):
    """缺表不能连累空壳检测 —— 那是另一条独立指标。"""
    db = tmp_path / "selfhosted.db"
    _make_db_without_write_ops(db, doc_text="对话记录", key_facts=[])
    monkeypatch.setitem(system.SYSTEM, "long_memory_db", str(db))

    report = system.health_check()
    assert report["stats"]["long_memory_hollow_recent"] == 1
    msgs = [p["msg"] for p in report["problems"] if p["area"] == "long_memory"]
    assert any("空壳" in m for m in msgs), msgs


def test_时间格式解析对得上真实库(use_lm):
    """documents.created_at 是本地时间字符串、memory_write_ops 是 epoch，别混。"""
    db = use_lm(last_doc_days_ago=1.0)
    stats = system._long_memory_stats()
    assert stats is not None
    assert stats["documents"] == 1
    assert stats["last_doc_ts"] is not None
    # 1 天前（允许 1 分钟误差），且是 epoch 秒级
    assert abs((time.time() - stats["last_doc_ts"]) - 86400) < 60


# --------------------------------------------------------------------------- #
# 空壳检测（2026-11-03 补）
#
# 生产实况：13 条 documents 的 text **全部**是字面量 "对话记录"，topics/key_facts
# 全空，而它们每条都有 completed 记录。旧体检只看条数增长，所以永远是绿灯。
# 这组用例钉住：写得进去 ≠ 写得有内容。
# --------------------------------------------------------------------------- #
def test_空壳记忆会被体检叫出来(use_lm):
    """正文只有 "对话记录"、key_facts 空 —— 这就是生产里那 13 条的样子。"""
    use_lm(last_doc_days_ago=0, doc_text="对话记录", key_facts=[])
    report = system.health_check()
    assert "long_memory" in _areas(report)
    msgs = [p["msg"] for p in report["problems"] if p["area"] == "long_memory"]
    assert any("空壳" in m for m in msgs), msgs
    assert report["stats"]["long_memory_hollow_recent"] == 1


def test_内容正常的记忆不报空壳(use_lm):
    use_lm(last_doc_days_ago=0)
    report = system.health_check()
    assert "long_memory" not in _areas(report)
    assert report["stats"]["long_memory_hollow_recent"] == 0


def test_正文够长但key_facts空也算空壳(use_lm):
    """摘要可能写了一大段却抽不出事实 —— 那也是没真的抽出内容。"""
    use_lm(last_doc_days_ago=0, doc_text="今天聊了很多东西但是什么都没记下来", key_facts=[])
    report = system.health_check()
    assert "long_memory" in _areas(report)
    assert report["stats"]["long_memory_hollow_recent"] == 1


def test_空壳统计只上最近20条(use_lm):
    """关心的是「现在还在不在写空壳」，不是历史包袱。

    注意取的是 **id desc**（写入顺序），不是 created_at —— 所以这里靠插入顺序构造：
    老空壳 25 条 → 最后再插一条正常内容。最近 20 条 = 正常那条 + 19 条空壳。
    """
    db = use_lm(last_doc_days_ago=30)  # fixture 那条是正常内容，但 id 最小
    import json as _json

    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 30 * 86400))
    normal_meta = _json.dumps({"key_facts": ["生日 10-20"]}, ensure_ascii=False)
    with sqlite3.connect(db) as conn:
        for i in range(25):
            conn.execute(
                "insert into documents(doc_id,text,metadata,created_at,updated_at)"
                " values(?,?,?,?,?)",
                (f"old{i}", "对话记录", _json.dumps({"key_facts": []}), stamp, stamp),
            )
        # 最新的一条是正常内容（id 最大）
        conn.execute(
            "insert into documents(doc_id,text,metadata,created_at,updated_at)"
            " values('new','晓说他生日是 10 月 20 日',?,?,?)",
            (normal_meta,) + (stamp,) * 2,
        )
        conn.commit()
    stats = system._long_memory_stats()
    assert stats["documents"] == 27
    assert stats["recent_checked"] == 20
    # 最近 20 条 = 最新那条正常内容 + 19 条老空壳
    assert stats["hollow_recent"] == 19
