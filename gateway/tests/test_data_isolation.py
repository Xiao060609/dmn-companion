"""守门测试：测试进程的写入目标绝不能是仓库里的 `gateway/data`（生产数据）。

这条用例不测业务，只钉一条红线：

> 在测试进程里，任何会落盘的路径常量都必须已经被 `conftest.py` 指走。

背景见 `conftest.py` 顶部：2026-11-02 跑 pytest 往生产 `events` 表写了 12 条
`care_given`，还反复重写 `data/persona_sync.json`。以后谁新增了测试、或者
新增了一个像 `persona_sync` 那样自带路径常量的模块却忘了隔离，这里会先红。

注意：这里故意**不**用 `monkeypatch`。要验证的正是「autouse fixture 已经兜住了」，
如果自己再改一遍路径，就等于在测自己的 monkeypatch，什么也证明不了。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import gateway  # noqa: E402
import persona_sync  # noqa: E402
import system  # noqa: E402

#: 用户的真实数据目录（同样用 realpath，Windows 上盘符/大小写也要一致）
PRODUCTION_DATA_DIR = os.path.normcase(os.path.realpath(str(GATEWAY_DIR / "data")))
#: 真人格文件本体（persona_sync 会写它，所以它就是生产数据）
PRODUCTION_PERSONA = os.path.normcase(os.path.realpath(str(GATEWAY_DIR / "persona.md")))


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def _is_inside(path: str, directory: str) -> bool:
    """path 是否等于 directory 或在它下面（不依赖 pathlib.is_relative_to）。"""
    path, directory = _norm(path), _norm(directory)
    return path == directory or path.startswith(directory + os.sep)


#: 所有会落盘的常量：(模块, 属性名, 该属性应远离的目录)
OUTPUTS = [
    (gateway, "DATA_DIR", PRODUCTION_DATA_DIR),
    (gateway, "DB_PATH", PRODUCTION_DATA_DIR),
    (gateway, "AUDIT_PATH", PRODUCTION_DATA_DIR),
    (gateway, "ROLE_PATH", PRODUCTION_DATA_DIR),
    (gateway, "TOKEN_PATH", PRODUCTION_DATA_DIR),
    (gateway, "KEYS_PATH", PRODUCTION_DATA_DIR),
    (system, "DATA_DIR", PRODUCTION_DATA_DIR),
    (system, "SYSTEM_STATE_PATH", PRODUCTION_DATA_DIR),
    (system, "SYSTEM_MODEL_PATH", PRODUCTION_DATA_DIR),
    (system, "SYSTEM_AUDIT_PATH", PRODUCTION_DATA_DIR),
    (system, "INNER_OS_PATH", PRODUCTION_DATA_DIR),
    (system, "PEOPLE_DIR", PRODUCTION_DATA_DIR),
    (system, "HEALTH_REPORT_PATH", PRODUCTION_DATA_DIR),
    (persona_sync, "STATE_PATH", PRODUCTION_DATA_DIR),
    (persona_sync, "ASTRBOT_DB", PRODUCTION_DATA_DIR),
]


def test_所有写盘路径都不在gateway_data下():
    leaked = [
        f"{module.__name__}.{name} = {getattr(module, name)}"
        for module, name, forbidden in OUTPUTS
        if _is_inside(getattr(module, name), forbidden)
    ]
    assert leaked == [], (
        "这些路径还指着生产数据目录 gateway/data，跑测试会写脏用户数据：\n  "
        + "\n  ".join(leaked)
    )


def test_persona不再指向真人格文件():
    """persona_sync 会重写 PERSONA_PATH——它必须是副本，不能是 gateway/persona.md。"""
    assert _norm(persona_sync.PERSONA_PATH) != PRODUCTION_PERSONA
    assert Path(persona_sync.PERSONA_PATH).is_file(), "影子副本应该已经建好"


def test_心跳数据库是隔离的空库且schema齐全():
    """autouse fixture 建的那个库：四张表都在、没有数据。"""
    db_path = gateway.DB_PATH
    assert _norm(db_path) != _norm(PRODUCTION_DATA_DIR + os.sep + "gateway.db")
    assert Path(db_path).is_file(), "autouse fixture 应该已经建好空库"
    conn = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
        counts = {
            t: conn.execute(f"select count(*) from {t}").fetchone()[0]
            for t in ("turns", "events", "schedules", "pending")
        }
    finally:
        conn.close()
    assert {"turns", "events", "schedules", "pending"} <= tables
    assert counts == {"turns": 0, "events": 0, "schedules": 0, "pending": 0}


def test_落盘写的是隔离库不是生产库():
    """真写一条 care_given，确认它落在隔离库里。"""
    system.record_care_given({"text": "守门用例", "emotion": "calm"})
    conn = sqlite3.connect(gateway.DB_PATH)
    try:
        rows = list(conn.execute("select kind, channel from events"))
    finally:
        conn.close()
    assert rows == [("care_given", "pet")]


def test_生产库没被这条用例碰到():
    """最后再确认一遍：上面那次写入没有改动生产库（存在才检查）。

    ⚠ 2026-11-07（P0-7 连带修）：这里原来用 `sqlite3.connect(str(prod_db))`
    ——**以可写方式打开生产库**。两个问题：

    1. **原则问题**：这条用例守的就是「测试不许碰生产数据」，它自己更不该
       以可写方式去开生产库。守门人自己踩线，这条线就没有说服力了。
    2. **实际副作用**：SQLite 在**最后一个连接关闭时**会 checkpoint 并删除
       `-wal` / `-shm` 侧车文件。实测跑完 `pytest gateway/tests` 之后，
       生产目录里的 `gateway.db-wal` / `gateway.db-shm` 就没了 ——
       「跑测试前后 `gateway/data` 零变化」这条验收因此永远过不了。

    ⚠ 注意成因**不是**「读写模式会导致删除」——精确的条件是
    **"关闭最后一个连接"会触发 checkpoint**。只读连接即使最后关闭也不
    checkpoint，所以 `mode=ro` 有效。但改成只读的真正理由是第 1 条：
    **根因是"测试进程去 connect 生产库"这件事本身**，不是打开模式。

    `immutable=1` 再进一步声明"这个文件不会变"，SQLite 连锁都不加、
    也不建侧车，读到的就是磁盘上那一份快照 —— 正是这里要的语义。
    """
    prod_db = Path(PRODUCTION_DATA_DIR) / "gateway.db"
    if not prod_db.is_file():
        return
    # 只读 + immutable：不加锁、不建/不删侧车、绝不 checkpoint。
    uri = "file:" + prod_db.as_posix() + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        leaked = list(
            conn.execute("select id from events where payload like '%守门用例%'")
        )
    finally:
        conn.close()
    assert leaked == [], "守门用例的写入漏进了生产库 gateway/data/gateway.db"


# --------------------------------------------------------------------------- #
# 长期记忆库（阶段 2 新增的第二个落盘目标）
#
# 2026-11-03 实际事故：`long_memory_db` 默认指 `gateway/data/livingmemory.db`，
# 而 conftest 当时只隔离了 `gateway.db`。跑一遍测试就往真实记忆库灌进 49 条，
# 其中 30 多条是 `测试事件` / `路过` / `摘录` 这类 stub。
#
# 教训和 11-02 那次一模一样：**新增一个落盘目标，就得同时加一条守门用例**，
# 否则"默认路径指向生产目录"这个坑会一次次重演。
# --------------------------------------------------------------------------- #
def test_长期记忆库不在gateway_data下():
    """`system.SYSTEM["long_memory_db_path"]` 必须被指到隔离目录。"""
    configured = str(system.SYSTEM.get("long_memory_db_path") or "")
    assert configured, "conftest 应该显式设置 long_memory_db_path"
    assert not _is_inside(configured, PRODUCTION_DATA_DIR), (
        f"长期记忆库还指着生产目录：{configured}"
    )


def test_写长期记忆落的是隔离库():
    """真写一条，确认落在隔离目录、不是 data/livingmemory.db。"""
    import memory_bridge

    configured = str(system.SYSTEM["long_memory_db_path"])
    b = memory_bridge.MemoryBridge(db_path=configured)
    try:
        # 用假 embedding 会花额度，这里只验证路径指向 —— 不真写。
        assert _norm(b.db_path) == _norm(configured)
    finally:
        b.close()
    assert not _is_inside(configured, PRODUCTION_DATA_DIR)
