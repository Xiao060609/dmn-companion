"""记忆层（`gateway/memory/`）的单测。

覆盖阶段 1 的验收点：
1. 写入 / 语义召回 / 关键词召回 / 混合检索 / 删除
2. 中文路径（faiss 的 fopen 坑 —— 必须走 ASCII 桥接）
3. **读插件的现有库**（表结构 1:1，不需要迁移）
4. `search_sparse` 的调用约定（收已分词列表，不是原始字符串）
5. 常驻事件循环桥的并发安全

测试不碰生产库：全部用 `tmp_path`。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
MEMORY_DIR = GATEWAY_DIR / "memory"
for p in (str(GATEWAY_DIR), str(MEMORY_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import loop_bridge  # noqa: E402
import store as store_mod  # noqa: E402
import tokenizer  # noqa: E402

DIM = 1024


class FakeEmbedding:
    """确定性伪向量：同一个文本**永远**得到同一个向量。

    用它把「向量层对不对」和「DashScope 通不通」两件事分开测。

    ⚠ 2026-11-08 修了两个坑：

    坑一：原来用 `abs(hash(text))` 播种，但 Python 的字符串 hash
    **每次进程启动都加随机盐**（除非设了 PYTHONHASHSEED），
    于是「同一个文本永远得到同一个向量」这句话实际是假的。
    实测：PYTHONHASHSEED=0 跑 5 次全过；不设种子跑 8 次挂 3 次。

    坑二（更要紧）：原来向量是**整句一个随机种子**出来的，和文本内容
    毫无关系 —— 那么「关键词命中的记录该排在前面」这件事根本测不了，
    因为向量榜的排序是纯噪声。加权融合里 VECTOR_WEIGHT=1.0，
    而关键词分只有 0.35/(60+rank)，噪声足以把关键词信号淹掉。
    测试当时"有时通过"只是碰运气。

    现在改成**词袋哈希**：按字符 bigram + 单字累加。共享字越多、
    向量越接近 —— 这样向量榜才真的反映文本相似度，
    "关键词优先"这类断言才有意义。
    """

    def __init__(self, dim: int = DIM) -> None:
        self._dim = dim
        self.calls = 0

    def get_dim(self) -> int:
        return self._dim

    def _vec(self, text: str) -> list[float]:
        v = np.zeros(self._dim, dtype=np.float32)
        text = (text or "").strip()
        grams = [text[i:i + 2] for i in range(max(len(text) - 1, 0))]
        grams.extend(text)          # 单字也计入，短文本才有信号
        if not grams:
            grams = [""]
        for g in grams:
            d = hashlib.blake2b(g.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(d[:4], "big") % self._dim
            v[idx] += 1.0 if d[4] & 1 else -1.0
        norm = float(np.linalg.norm(v))
        if norm > 0:
            v /= norm
        return v.tolist()

    async def get_embedding(self, text: str) -> list[float]:
        self.calls += 1
        return self._vec(text)

    async def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        self.calls += len(texts)
        return [self._vec(t) for t in texts]

    async def get_embeddings_batch(self, texts, batch_size: int = 32):
        return await self.get_embeddings(texts)


@pytest.fixture()
def mem(tmp_path):
    """一个临时记忆库。"""
    s = store_mod.open_store(
        db_path=str(tmp_path / "记忆库" / "livingmemory.db"),  # 故意用中文路径
        dim=DIM,
        embedding=FakeEmbedding(),
    )
    yield s
    s.close()


# --------------------------------------------------------------------------- #
# 1. 基本读写
# --------------------------------------------------------------------------- #
def test_写入并计数(mem):
    assert mem.count() == 0
    mem.add("主人讨厌人群和应酬；讨厌人群；应酬", {"importance": 0.8})
    assert mem.count() == 1


def test_语义检索能召回自己(mem):
    mem.add("小鱼的生日是11月1日；生日；11月1日", {"importance": 0.9})
    mem.add("主人喜欢深夜写代码；写代码；深夜", {"importance": 0.5})
    hits = mem.search("小鱼的生日是11月1日；生日；11月1日", top_k=1)
    assert hits and "生日" in hits[0]["text"]


def test_关键词检索命中精确词(mem):
    """精确词（生日 / 10月20日）是 FTS 的价值所在，纯向量容易漏。"""
    mem.add("主人的生日是10月20日；生日；10月20日", {"importance": 0.9})
    mem.add("主人喜欢深夜写代码；写代码", {"importance": 0.5})
    hits = mem.search_keyword("10月20日", limit=5)
    assert hits, "精确日期应该能命中"
    assert any("10月20日" in h["text"] for h in hits)


def test_关键词检索命中中文词(mem):
    mem.add("主人讨厌人群和应酬；人群；应酬", {"importance": 0.7})
    hits = mem.search_keyword("人群", limit=5)
    assert hits and any("人群" in h["text"] for h in hits)


def test_混合检索关键词优先(mem):
    """精确词命中的记录应该排在最前面。

    ⚠ 2026-11-07 改了断言口径（原来是 `source == "keyword"`）。
    原来 `search_hybrid` 是「关键词结果整段拼在向量结果前面」，
    所以第一名必然是 keyword。但这个实现有两个问题：

      1. BM25 原始分（负数）和向量分（0~1）量纲不同，混排没有可比性
      2. 纯 RRF 只看排名，会把强信号压平 —— 实测两个榜第一名互换时
         算出完全相等的分，排序退化成"谁先插入谁在前"

    现在改成加权融合（向量分保留强度 + 关键词排名补充），
    所以**精确词命中的那条会同时出现在两个榜里 → `source == "both"`**。
    "精确词排前面"的意图不变，只是标记从 keyword 变成了 both。
    """
    mem.add("主人的生日是10月20日；生日；10月20日", {"importance": 0.9})
    mem.add("今天聊了很多别的事；杂谈", {"importance": 0.3})
    merged = mem.search_hybrid("生日", top_k=3)
    assert merged
    # 验**意图**而不是实现：精确词命中的那条该排第一。
    #
    # ⚠ 2026-11-08 又改了一次：上一版断言 `source in ("keyword", "both")`，
    # 但在加权融合下，如果向量分足够强，这条可能**只**被向量榜命中
    # （关键词榜里的中文分词 + BM25 短文档惩罚会让它落到第二）。
    # 来源标签是实现细节，内容才是意图。所以这里直接看内容。
    assert "10月20日" in merged[0]["text"] or "生日" in merged[0]["text"], (
        f"精确词命中的记录应该排第一，实际是 {merged[0]['text']!r}")


def test_混合检索_两种来源都标出来(mem):
    """两个榜都命中的记录标 `both` —— 这是"精确词 + 语义都接近"的信号。"""
    mem.add("主人的生日是10月20日；生日；10月20日", {"importance": 0.9})
    merged = mem.search_hybrid("生日", top_k=3)
    assert merged
    assert merged[0]["source"] == "both"


# 注：关于「强向量信号不该被弱关键词压过」的用例**没有**写在这里。
#
# 原因：本文件用的是 `FakeEmbedding`（确定性哈希伪向量），它**没有语义** ——
# 「我喜欢什么歌」和「小菲鱼最喜欢的歌曲」之间的向量距离是随机的。
# 在这上面断言"向量强度应该胜出"测的是伪随机数，不是检索逻辑。
#
# 那条行为已经在**真实 bge** 上验证过（2026-11-07）：
#   查询「我喜欢什么歌」
#     关键词榜  1. 主人喜欢灰色      2. DOUDOU 那首歌
#     向量榜    1. DOUDOU(0.481)    2. 主人喜欢灰色(0.213)
#   纯 RRF 融合 → 两榜第一互换 → 分数完全相等 → 排序退化成先插入者优先（错）
#   加权融合   → 0.487 vs 0.419 → 正确命中 DOUDOU ✅
#
# 要把它固化成回归用例，需要一个**有语义的**假 embedding（比如给几条固定
# 文本硬编码向量）。那是另一件事，记在这里免得下一个人以为漏测了。


def test_删除后不再返回(mem):
    mem.add("要被删掉的记忆；删除测试", {"importance": 0.5})
    kept = mem.add("要留着的记忆；保留测试", {"importance": 0.5})
    assert mem.count() == 2

    doc_id = None
    for hit in mem.search_keyword("删除测试", limit=5):
        doc_id = hit["doc_id"]
    assert doc_id, "应该先能查到"
    assert mem.delete(doc_id) is True
    assert mem.count() == 1
    assert kept


# --------------------------------------------------------------------------- #
# 2. 中文路径（faiss 的 fopen 坑）
# --------------------------------------------------------------------------- #
def test_中文路径下索引能落盘并重读(tmp_path):
    """faiss 的 C++ fopen 按 ANSI 读路径，中文路径直写会挂。

    `embedding_storage` 自带 ASCII 桥接（path.isascii() 自动判断），
    本用例钉住"中文路径下依然能写、能读回来"。
    """
    db = tmp_path / "中文目录" / "记忆库.db"
    idx = tmp_path / "中文目录" / "记忆库.index"
    s = store_mod.open_store(
        db_path=str(db), index_path=str(idx), dim=DIM, embedding=FakeEmbedding()
    )
    try:
        s.add("中文路径测试记忆；路径测试", {"importance": 0.5})
        assert idx.exists(), "索引文件应该真的落盘"
        assert idx.stat().st_size > 0
        # 重建一个 store 从磁盘读回
        s.close()
        s2 = store_mod.open_store(
            db_path=str(db), index_path=str(idx), dim=DIM, embedding=FakeEmbedding()
        )
        try:
            assert s2.count() == 1
            hits = s2.search("中文路径测试记忆；路径测试", top_k=1)
            assert hits, "重读后仍应能检索"
        finally:
            s2.close()
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# 3. 调用约定：search_sparse 收已分词列表
# --------------------------------------------------------------------------- #
def test_search_sparse收的是分词列表不是原串():
    """踩过的坑：传原串会被逐字符拆开（"人群" → "人" OR "群"）。

    这不是 bug，是调用约定；`MemoryStore.search_keyword` 已经替调用方分好词。
    本用例钉住底层行为，避免以后有人"顺手优化"成传原串。
    """
    from tokenizer import build_fts5_or_query

    assert build_fts5_or_query(["人群"]) == '"人群"'
    # 传字符串时逐字符遍历 —— 这就是错用时的表现
    assert build_fts5_or_query("人群") == '"人" OR "群"'


def test_分词会保留有意义的词():
    sw = {"的", "是", "了"}
    toks = tokenizer.to_fts5_search_text("主人的生日是10月20日", sw).split()
    assert "生日" in toks
    assert "的" not in toks


# --------------------------------------------------------------------------- #
# 4. 读插件的现有库（表结构 1:1）
# --------------------------------------------------------------------------- #
def _make_plugin_like_db(path: Path, n: int = 3) -> list[str]:
    """造一个与插件 `documents` 表同构的库。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    ids: list[str] = []
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            "create table documents("
            " id integer primary key autoincrement,"
            " doc_id text not null unique,"
            " text text not null,"
            " metadata text,"
            " created_at datetime,"
            " updated_at datetime)"
        )
        for i in range(n):
            doc_id = f"plugin-doc-{i}"
            ids.append(doc_id)
            conn.execute(
                "insert into documents(doc_id,text,metadata,created_at,updated_at)"
                " values(?,?,?,?,?)",
                (
                    doc_id,
                    f"插件的第{i}条记忆；事实{i}",
                    json.dumps(
                        {
                            "session_id": "snowluma-onebot:FriendMessage:10000001",
                            "importance": 0.7,
                            "status": "active",
                        },
                        ensure_ascii=False,
                    ),
                    "2026-11-03 01:00:00",
                    "2026-11-03 01:00:00",
                ),
            )
        conn.commit()
    return ids


def test_能直接读插件同构的库(tmp_path):
    """表结构 1:1 —— 这是"不迁移就能接管"的前提。"""
    db = tmp_path / "plugin_like.db"
    expect_ids = _make_plugin_like_db(db, 3)

    s = store_mod.open_store(db_path=str(db), dim=DIM, embedding=FakeEmbedding())
    try:
        assert s.count() == 3
        hits = s.search_keyword("事实1", limit=5)
        assert hits, "老数据的 FTS 索引应能被建起来并检索"
        # 逐条对账
        with sqlite3.connect(str(db)) as conn:
            got = {r[0] for r in conn.execute("select doc_id from documents")}
        assert got == set(expect_ids)
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# 5. metadata 形状统一（真实环境才暴露的坑）
# --------------------------------------------------------------------------- #
def test_两条检索路径的metadata都是dict(mem):
    """向量那条回来是 JSON 字符串、FTS 那条是 dict —— 必须统一。

    真实环境踩过：`AttributeError: 'str' object has no attribute 'get'`。
    单测里因为 FakeEmbedding 走的是同一条内存路径，第一版没暴露出来。
    """
    mem.add("主人讨厌人群；人群；讨厌人群", {"importance": 0.8, "session_id": "owner"})

    for hits in (
        mem.search("主人讨厌人群；人群；讨厌人群", top_k=1),
        mem.search_keyword("人群", limit=3),
    ):
        assert hits, "两条路径都该有命中"
        for hit in hits:
            assert isinstance(hit["metadata"], dict), (
                f"metadata 应该是 dict，实际 {type(hit['metadata']).__name__}"
            )
            assert hit["metadata"].get("session_id") == "owner"


# --------------------------------------------------------------------------- #
# 6. 事件循环桥
# --------------------------------------------------------------------------- #
def test_桥能重复取到同一个实例():
    a = loop_bridge.get_bridge()
    b = loop_bridge.get_bridge()
    assert a is b, "必须单例 —— 多个循环会各持一份 faiss 索引"


def test_桥并发安全(tmp_path):
    """心跳 / 巩固 / HTTP 三个线程会同时写记忆。"""
    import threading

    s = store_mod.open_store(
        db_path=str(tmp_path / "并发.db"), dim=DIM, embedding=FakeEmbedding()
    )
    errors: list[str] = []

    def worker(tag: str) -> None:
        for i in range(5):
            try:
                s.add(f"并发{tag}{i}；并发测试", {"importance": 0.5})
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{tag}{i}: {type(exc).__name__}: {exc}")

    try:
        threads = [threading.Thread(target=worker, args=(t,)) for t in ("a", "b", "c")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert not errors, f"并发写入出错: {errors[:3]}"
        assert s.count() == 15
    finally:
        s.close()

# --------------------------------------------------------------------------- #
# 索引一致性（2026-11-09）
#
# 背景：`documents`（sqlite）和 faiss 向量索引**无法共用一个事务**，
# 只能写补偿逻辑。补偿失败（进程被杀、断电）会把两边永久劈开，
# 而且**不报任何错** —— 召回只是悄悄变差。
# 实测栽过两次：documents 7 行、index 56 个向量，孤儿把 top-k 名额占满，
# 语义召回命中真记忆 0 条。
# --------------------------------------------------------------------------- #
def test_新建库对账是一致的(mem):
    c = mem.consistency()
    assert c["docs"] == 0
    assert c["vectors"] == 0
    assert c["orphans"] == []
    assert c["missing"] == []
    assert c["ok"] is True


def test_写入后对账仍然一致(mem):
    mem.add("主人喜欢灰色", {"importance": 0.6})
    mem.add("主人大三学生", {"importance": 0.6})
    c = mem.consistency()
    assert c["docs"] == 2
    assert c["vectors"] == 2
    assert c["ok"] is True


def test_手动删库会产生孤儿向量(mem):
    """直接删 documents 行、不动 faiss —— 模拟"补偿失败"的现场。"""
    mem.add("主人喜欢灰色", {"importance": 0.6})
    mem.add("主人大三学生", {"importance": 0.6})

    # 绕过 store.delete()（那个会同时删向量），直接动 sqlite
    import sqlite3

    conn = sqlite3.connect(mem.db_path)
    try:
        conn.execute("delete from documents where rowid = "
                     "(select min(rowid) from documents)")
        conn.commit()
    finally:
        conn.close()

    c = mem.consistency()
    assert c["docs"] == 1
    assert c["vectors"] == 2, "向量数应该还是 2"
    assert len(c["orphans"]) == 1, "应该报出 1 个孤儿向量"
    assert c["ok"] is False


def test_drop_orphan_vectors清掉孤儿(mem):
    mem.add("主人喜欢灰色", {"importance": 0.6})
    mem.add("主人大三学生", {"importance": 0.6})

    import sqlite3

    conn = sqlite3.connect(mem.db_path)
    try:
        conn.execute("delete from documents where rowid = "
                     "(select min(rowid) from documents)")
        conn.commit()
    finally:
        conn.close()

    assert mem.consistency()["ok"] is False
    n = mem.drop_orphan_vectors()
    assert n == 1, "应该删掉 1 个孤儿"
    assert mem.consistency()["ok"] is True


def test_drop_orphan_vectors没孤儿时返回0(mem):
    mem.add("主人喜欢灰色", {"importance": 0.6})
    assert mem.drop_orphan_vectors() == 0


def test_rebuild_vectors修好缺失向量(mem):
    """`documents` 有行、faiss 没向量 —— 这条记忆等于检索不到。

    `drop_orphan_vectors()` 处理不了这个方向（它只删多余的），
    必须靠 `rebuild_vectors()` 重算。
    """
    mem.add("主人喜欢灰色", {"importance": 0.6})
    mem.add("主人大三学生", {"importance": 0.6})

    # 清空向量索引，保留 documents
    mem._bridge.run(mem.db.embedding_storage.delete(
        mem.db.embedding_storage.all_ids()
    ))

    c = mem.consistency()
    assert c["docs"] == 2
    assert c["vectors"] == 0, "向量被清空了"
    assert len(c["missing"]) == 2, "应该报出 2 个缺失"
    assert c["ok"] is False

    res = mem.rebuild_vectors()
    assert res["rebuilt"] == 2
    assert res["after"]["ok"] is True, "重建后应该一致"
    assert res["after"]["vectors"] == 2


def test_rebuild_vectors同时修好孤儿和缺失(mem):
    """两个方向都坏的情况下，一次重建全部修好。"""
    mem.add("主人喜欢灰色", {"importance": 0.6})
    mem.add("主人大三学生", {"importance": 0.6})
    mem.add("主人住重庆", {"importance": 0.6})

    import sqlite3

    # 删一行 documents（造孤儿）
    conn = sqlite3.connect(mem.db_path)
    try:
        conn.execute("delete from documents where rowid = "
                     "(select min(rowid) from documents)")
        conn.commit()
    finally:
        conn.close()

    c = mem.consistency()
    assert len(c["orphans"]) == 1
    assert c["ok"] is False

    res = mem.rebuild_vectors()
    assert res["after"]["ok"] is True
    assert res["after"]["docs"] == 2
    assert res["after"]["vectors"] == 2
    assert res["rebuilt"] == 2


def test_rebuild_vectors空库也不炸(mem):
    """库是空的时候重建，不能抛异常。"""
    res = mem.rebuild_vectors()
    assert res["rebuilt"] == 0
    assert mem.consistency()["ok"] is True


def test_rebuild_vectors保留原有向量内容(mem):
    """重建之后，检索结果不能变 —— 否则就等于把记忆写坏了。"""
    mem.add("主人喜欢灰色", {"importance": 0.6})
    mem.add("主人住重庆", {"importance": 0.6})

    before = {h["text"] for h in mem.search("灰色", top_k=2)}
    mem.rebuild_vectors()
    after = {h["text"] for h in mem.search("灰色", top_k=2)}

    assert before == after, "重建前后检索结果不一致"
