"""`MemoryStore` —— 记忆层的统一门面。

为什么要门面
------------
搬过来的 `FaissVecDB` / `DocumentStorage` 是 async 的，而且职责分散
（一个管向量、一个管文档 + FTS）。网关那边只想要三个动作：

    写一条记忆 → 按语义找 → 按关键词找

这里把它们包成一个同步对象，内部用 `loop_bridge` 驱动，调用方不用碰 asyncio。

数据形状（与插件一致，不要改）
------------------------------
`documents` 表：
    id / doc_id / text / metadata(JSON) / created_at / updated_at
`documents_fts`（FTS5 虚拟表）：rowid 对齐 documents.id，列 search_text

写入时 `text` 是**给检索用的富文本**（摘要 + 关键事实拼起来），
不是原始对话 —— 原始对话在网关的 `turns` 里。
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid

from dashscope_embedding import DashScopeEmbedding
from document_storage import DocumentStorage
from loop_bridge import get_bridge
from provider import NullRerankProvider
from tokenizer import to_fts5_search_text
from vec_db import FaissVecDB

logger = logging.getLogger("xiaoyu.memory")

DEFAULT_DIM = 1024


def _async(fn):
    """把一个**无参同步工厂**包成协程，好投递进事件循环线程执行。

    为什么要这么绕：`bridge.run()` 只收协程，但 `FaissVecDB(...)` 是同步构造。
    直接在线程外构造会让 faiss 句柄诞生在调用者线程，而后续所有 await
    都发生在循环线程 —— 跨线程持有 C++ 对象是隐患，统一在循环线程里建更安全。

    注意入参是**函数**不是函数调用结果（`_async(factory)`，不是 `_async(factory())`）。
    """

    async def _run():
        return fn()

    return _run()


def _as_dict(value) -> dict:
    """把 metadata 统一成 dict。

    `documents.metadata` 是 TEXT 列，不同检索路径回来的形状不一样：
    向量那条是 JSON 字符串，FTS 那条是已解析的 dict。
    统一在这里处理，调用方不用关心来源。
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _default_embedding(dim: int):
    """按配置选向量化实现：本地 bge 还是 DashScope API。

    读取 `gateway/config.json` 的 `system.embedding_provider`：
      * `"dashscope"`（默认）—— 联网 + 按次计费，`text-embedding-v4`
      * `"local"`               —— 本地 bge-large-zh-v1.5，零成本、不出机器

    ⚠ **两条路径的向量空间完全不同，不能混用**。
    切换时必须**全量重算已有记忆的向量**，否则同一个索引里躺着两个模型的
    向量，余弦相似度没有意义，召回会彻底失效。

    ⚠ 这个函数在任何异常下都退回 DashScope，**不让配置问题炸掉记忆库**：
    宁可多花钱，不可整条记忆链路起不来。
    """
    provider = "dashscope"
    cfg: dict = {}
    try:
        cfg_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"
        )
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = (json.load(fh) or {}).get("system") or {}
        provider = str(cfg.get("embedding_provider") or "dashscope").strip().lower()
    except Exception:  # noqa: BLE001 —— 配置读不到就用默认
        provider = "dashscope"

    if provider == "local":
        try:
            from local_embedding import DEFAULT_DIM, LocalEmbedding

            loc = cfg.get("local_embedding") or {}
            return LocalEmbedding(
                model_name=str(loc.get("model_name") or "BAAI/bge-large-zh-v1.5"),
                cache_dir=str(loc.get("cache_dir") or r"G:\models") or None,
                device=str(loc.get("device") or "cpu"),
                dim=int(loc.get("dim") or DEFAULT_DIM or dim),
            )
        except Exception as exc:  # noqa: BLE001 —— 本地模型起不来就退回 API
            logger.warning("本地 embedding 初始化失败，退回 DashScope: %s", exc)

    return DashScopeEmbedding(dim=dim)


class MemoryStore:
    """同步门面：写 / 语义检索 / 关键词检索 / 删除。

    Args:
        db_path: `documents` 所在的 sqlite 文件（与插件的 livingmemory.db 同构）。
        index_path: faiss 索引文件路径。默认与 db 同目录同名 `.index`。
        embedding: 向量化实现。默认 `DashScopeEmbedding`（1024 维）。
        dim: 向量维度。**必须与已有索引一致**，否则读不了旧索引。

    注意：本对象**不是线程安全的**，但内部所有 async 调用都投递到同一个
    事件循环线程，所以多线程调用是安全的（实验已验证 3 线程并发零错误）。
    """

    def __init__(
        self,
        db_path: str,
        index_path: str | None = None,
        embedding=None,
        dim: int = DEFAULT_DIM,
        rerank=None,
    ) -> None:
        self.db_path = str(db_path)
        self.index_path = index_path or os.path.splitext(self.db_path)[0] + ".index"
        self._dim = int(dim)
        self._embedding = embedding or _default_embedding(self._dim)
        self._rerank = rerank or NullRerankProvider()
        self._bridge = get_bridge()
        self._db: FaissVecDB | None = None

    # ------------------------------------------------------------------ 生命周期
    def open(self) -> "MemoryStore":
        """建/打开库与索引。幂等。"""
        if self._db is not None:
            return self
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)) or ".", exist_ok=True)

        # 注意：`FaissVecDB.__init__` 是**同步**构造函数（只是拼对象，不 await），
        # 不能塞进 bridge.run() —— 那里只收协程。只有 `initialize()` 是 async。
        # 但构造函数内部会建 faiss 索引对象，仍属于"记忆层状态"，
        # 必须在事件循环线程里创建，避免跨线程持有 faiss 句柄。
        def _build() -> FaissVecDB:
            return FaissVecDB(
                doc_store_path=self.db_path,
                index_store_path=self.index_path,
                embedding_provider=self._embedding,
                rerank_provider=self._rerank,
            )

        self._db = self._bridge.run(_async(_build))
        self._bridge.run(self._db.initialize())
        return self

    def close(self) -> None:
        """关闭（只关本对象持有的库，不关全局循环）。"""
        if self._db is None:
            return
        try:
            self._bridge.run(self._db.close())
        except Exception:  # noqa: BLE001 —— 关不掉不该抛穿
            logger.warning("关闭记忆库失败", exc_info=True)
        finally:
            self._db = None

    def __enter__(self) -> "MemoryStore":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def db(self) -> FaissVecDB:
        if self._db is None:
            self.open()
        assert self._db is not None
        return self._db

    # ------------------------------------------------------------------ 写
    def add(
        self,
        text: str,
        metadata: dict | None = None,
        doc_id: str | None = None,
    ) -> int:
        """写入一条记忆，返回内部自增 id。

        Args:
            text: **给检索用的富文本**。建议格式「摘要句；关键事实1；关键事实2」，
                  不要塞原始对话 —— 那样向量会被稀释，关键词也会误命中。
            metadata: 任意 JSON 可序列化字段（session_id / importance / topics /
                  key_facts 等）。与插件的字段名保持一致，面板才能读。
            doc_id: 外部 ID。默认生成 UUID。
        """
        meta = dict(metadata or {})
        meta.setdefault("create_time", time.time())
        meta.setdefault("status", "active")
        return self._bridge.run(
            self.db.insert(content=text, metadata=meta, id=doc_id or str(uuid.uuid4()))
        )

    # ------------------------------------------------------------------ 读
    def search(self, query: str, top_k: int = 5, fetch_k: int = 20) -> list[dict]:
        """语义检索（向量）。返回 `[{"id","doc_id","text","metadata","score"}]`。

        注意底层 `FaissVecDB.retrieve` 的条数参数叫 `k`（不是 `top_k`），
        这里做一次转换，对外统一用 `top_k`（和插件 `search_memories` 一致）。
        """
        results = self._bridge.run(
            self.db.retrieve(query=query, k=top_k, fetch_k=fetch_k, rerank=False)
        )
        out: list[dict] = []
        for item in results or []:
            data = dict(getattr(item, "data", {}) or {})
            # 统一 metadata 形状：`documents.metadata` 是 TEXT 列，这里回来的是
            # **JSON 字符串**；`search_keyword()` 那条已经解析成 dict。
            # 两条检索路径形状必须一致，否则调用方要写两套判断
            # （真实环境踩过：`AttributeError: 'str' object has no attribute 'get'`）。
            data["metadata"] = _as_dict(data.get("metadata"))
            data["score"] = float(getattr(item, "similarity", 0.0))
            out.append(data)
        return out

    def search_keyword(self, query: str, limit: int = 5) -> list[dict]:
        """关键词检索（FTS5 / BM25）。精确词（"生日"、"10月20日"）靠这条。

        ⚠ `DocumentStorage.search_sparse` 收**已分词列表**，这里替调用方分好。
        """
        store = self.db.document_storage
        tokens = to_fts5_search_text(query, store.stopwords).split()
        if not tokens:
            return []
        rows = self._bridge.run(store.search_sparse(tokens, limit=limit)) or []
        out: list[dict] = []
        for row in rows:
            out.append(
                {
                    "id": row.get("id"),
                    "doc_id": row.get("doc_id"),
                    "text": row.get("text"),
                    "metadata": _as_dict(row.get("metadata")),
                    "score": float(row.get("score") or 0.0),
                }
            )
        return out

    def search_hybrid(
        self,
        query: str,
        top_k: int = 5,
        keyword_limit: int = 5,
    ) -> list[dict]:
        """混合检索：关键词 + 向量，按**归一化分数**融合后排序。

        为什么需要融合而不是简单拼接（2026-11-07 修）
        --------------------------------------------
        原来的实现是「关键词结果全排在向量结果前面」，两个池子拼起来就返回。
        实测出两个问题：

          1. **量纲不同直接混排是错的**：关键词那条用的是 BM25 原始分
             （负数，越负越相关），向量那条是 0~1 的余弦相似度。
             两类分数放在一个列表里没有可比性。

          2. **关键词没命中时，全靠向量**：`search_keyword` 返回空，
             列表里就只有向量的 top-k。实测「我喜欢什么歌」被答成
             「主人喜欢灰色」（0.39）—— 因为它是向量 top1，
             而真正该命中的「DOUDOU 那首歌」排在后面。

        现在的做法（RRF，Reciprocal Rank Fusion）
        ----------------------------------------
        不比较两种分数的绝对值，只比较**排名**：
            score = Σ 1/(k + rank)        k=60（业界常用值）

        RRF 的好处正是不需要归一化 —— 两种检索的分数尺度完全无关也能融合，
        因为它只看"在各自榜单里排第几"。而且一条记忆如果**在两个榜单里都靠前**，
        天然会得到更高分，这正是我们要的："精确词命中 + 语义也接近"的应该排最前。

        Args:
            query: 查询文本。
            top_k: 向量侧取多少条。
            keyword_limit: 关键词侧取多少条。

        Returns:
            `[{"id","doc_id","text","metadata","score","source"}]`，
            `source` 取值 `keyword` / `vector` / `both`，按融合分降序。
        """
        # 加权 RRF：向量分保留**强度信息**，关键词只做排名补充。
        #
        # 为什么不是纯 RRF（2026-11-07 实测后改）
        # -------------------------------------
        # 纯 RRF 只看排名，会把强信号压平。实测「我喜欢什么歌」：
        #     关键词榜  1. 主人喜欢灰色      2. DOUDOU 那首歌
        #     向量榜    1. DOUDOU(0.481)    2. 主人喜欢灰色(0.213)
        # 两个榜第一名互换 → 纯 RRF 算出 **完全相等的分**（0.03252 vs 0.03252），
        # 排序退化成"谁先插入谁在前"。
        #
        # 但向量那一侧的信息量明显更大：0.481 vs 0.213 差 2.3 倍，
        # 而关键词那边只是"都命中了一个词"。
        #
        # 所以：**向量分 × 权重 + 关键词排名分 × 小权重**。
        # 向量分天然带强度，关键词分只回答"这个词精确出现了没有"。
        #
        # 权重依据：这个项目的召回设计是「关键词优先、向量补齐」
        # （见模块 README），所以关键词要有存在感；
        # 但实测表明**纯按关键词排名压过向量强度会出错**，所以向量权重更高。
        VECTOR_WEIGHT = 1.0
        KEYWORD_WEIGHT = 0.35
        RRF_K = 60.0
        scores: dict[str, float] = {}
        items: dict[str, dict] = {}
        sources: dict[str, set[str]] = {}

        def _key(item: dict) -> str:
            return str(item.get("doc_id") or item.get("id") or "")

        # 关键词榜：只有排名，没有可比的分值（BM25 原始分是负数且量纲不同）
        for rank, item in enumerate(self.search_keyword(query, limit=keyword_limit), 1):
            k = _key(item)
            if not k:
                continue
            scores[k] = scores.get(k, 0.0) + KEYWORD_WEIGHT / (RRF_K + rank)
            items.setdefault(k, item)
            sources.setdefault(k, set()).add("keyword")

        # 向量榜：用**真实相似度**（0~1，可比），不退化成分数
        for item in self.search(query, top_k=top_k):
            k = _key(item)
            if not k:
                continue
            sim = float(item.get("score") or 0.0)
            scores[k] = scores.get(k, 0.0) + VECTOR_WEIGHT * sim
            items.setdefault(k, item)
            sources.setdefault(k, set()).add("vector")

        out: list[dict] = []
        for k in sorted(scores, key=lambda x: scores[x], reverse=True):
            item = dict(items[k])
            item["score"] = round(scores[k], 6)
            tag = sources.get(k, set())
            item["source"] = "both" if len(tag) == 2 else next(iter(tag), "vector")
            out.append(item)
        return out[: max(top_k, keyword_limit)]

    def count(self) -> int:
        return int(self._bridge.run(self.db.document_storage.count_documents({})) or 0)

    def list_documents(self, limit: int = 2000) -> list[dict]:
        """全量拉文档（遗忘扫描用）。返回 `[{"doc_id","text","metadata"}]`。

        和 `prune()` 内部那次读是同一份数据，但这里把形状统一成
        `forgetting.plan_prune()` 吃的形状，调用方不用再自己转。
        """
        docs = self._bridge.run(
            self.db.document_storage.get_documents({}, limit=limit)
        ) or []
        out: list[dict] = []
        for doc in docs:
            out.append(
                {
                    "doc_id": str(doc.get("doc_id") or ""),
                    "text": str(doc.get("text") or ""),
                    "metadata": _as_dict(doc.get("metadata")),
                }
            )
        return out

    def touch_recall(self, doc_id: str, now: float | None = None) -> bool:
        """记一次「这条记忆被召回」：`use_count += 1` / `last_used = now`。

        为什么必须有这个（`plan/DMN审核.md` 3.7 的「前置条件」）：

            查过代码：召回一条记忆时，除了把内容塞进 prompt，**什么都没记** ——
            没记次数、没记时间。所以要用这套算法，第一步得先加这个记录。
            没有它，「越用越熟」就是空话。

        实现上走 `update_document_by_doc_id` 会**顺手重建 FTS 行**（没必要，
        文本根本没变），所以这里直接改 metadata 那一列：读 → 合并 → 写。
        失败返回 False（召回统计坏了绝不能拦这一轮对话）。
        """
        key = str(doc_id or "").strip()
        if not key:
            return False
        stamp = time.time() if now is None else float(now)
        store = self.db.document_storage

        async def _bump():
            doc = await store.get_document_by_doc_id(key)
            if not doc:
                return False
            meta = _as_dict(doc.get("metadata"))
            try:
                count = int(meta.get("use_count") or 0)
            except (TypeError, ValueError):
                count = 0
            meta["use_count"] = count + 1
            meta["last_used"] = stamp
            async with store.get_session() as session:
                from sqlalchemy import text as sa_text

                await session.execute(
                    sa_text("update documents set metadata=:m where doc_id=:d"),
                    {"m": json.dumps(meta, ensure_ascii=False), "d": key},
                )
                await session.commit()
            return True

        try:
            return bool(self._bridge.run(_bump()))
        except Exception:  # noqa: BLE001 —— 统计失败不拦召回
            logger.warning("记录召回失败 doc_id=%s", key, exc_info=True)
            return False

    def bump_stability(self, doc_id: str, stability_days: float,
                       now: float | None = None) -> bool:
        """把一条记忆的稳定性拉高到 `stability_days`，落到 `metadata.stability_days`。

        为什么需要这个（P0-6，2026-11-07 审计）
        --------------------------------------
        `forgetting.plan_prune()` 对「永不删类型 + R 掉到 `drop_below` 以下」
        的条目会算出 `boost_to_stability_days`，注释写着"拉高稳定性，下次扫描
        R 就回到门槛上"。但 `memory_bridge.forget_plan()` **只读了计数
        （`stats["boosted"]`），从没把值写回 metadata** —— 于是：

          * 不删（对，这是"永不删"该有的行为）；
          * 稳定性也不涨（错） → 下次扫描 R 还是低 → 每次都重新算一遍 boost。

        净效果是**这条保护只剩半条**：`promise` / `birthday` 保住了"不删"，
        但"越久越该稳固"这一半是空转的。当前生产 `boosted` 恒 0（记忆都还年轻）
        所以暂未出事，但这是**假保护**，得补上。

        实现口径和 `touch_recall()` **完全一致**：只改 metadata 那一列，
        不碰 text、不重建 FTS（文本没变，重建是白干活 + 有写坏的风险）。

        ⚠ 为什么要写 `stability_days` 而不是别的键：
        `forgetting.stability_for()` 的**优先级①**就是
        `meta["stability_days"]`（显式数值 >0 就用它），所以写这里下次扫描
        立刻生效。写个别名键会看起来"存下来了"但根本不参与计算 —— 那是
        更坏的假修复。

        Args:
            doc_id: 文档 id。
            stability_days: 要拉高到的稳定性（天）。必须 > 0，否则不动
                （`None` 在 `stability_for` 里表示"永不删"，语义不同，别混）。
            now: 记录时间戳，缺省 `time.time()`。

        Returns:
            True = 真的写进去了；False = doc 不存在 / 参数非法 / 写失败。
            失败**不抛** —— 遗忘扫描是每日后台链路，一条写不进去不该整批停。
        """
        key = str(doc_id or "").strip()
        if not key:
            return False
        try:
            target = float(stability_days)
        except (TypeError, ValueError):
            return False
        if not (target > 0.0):
            return False
        stamp = time.time() if now is None else float(now)
        store = self.db.document_storage

        async def _bump():
            doc = await store.get_document_by_doc_id(key)
            if not doc:
                return False
            meta = _as_dict(doc.get("metadata"))
            # 已经比目标更高就别压低它（boost 只往上拉，不是赋值）
            try:
                current = float(meta.get("stability_days") or 0.0)
            except (TypeError, ValueError):
                current = 0.0
            if current >= target:
                return False
            meta["stability_days"] = target
            meta["stability_boosted_at"] = stamp
            async with store.get_session() as session:
                from sqlalchemy import text as sa_text

                await session.execute(
                    sa_text("update documents set metadata=:m where doc_id=:d"),
                    {"m": json.dumps(meta, ensure_ascii=False), "d": key},
                )
                await session.commit()
            return True

        try:
            return bool(self._bridge.run(_bump()))
        except Exception:  # noqa: BLE001 —— 拉高稳定性失败不拦整批遗忘
            logger.warning("拉高稳定性失败 doc_id=%s", key, exc_info=True)
            return False

    # ------------------------------------------------------------------ 删
    def delete(self, doc_id: str) -> bool:
        """删除一条记忆。返回**是否真的删掉了**。

        底层 `FaissVecDB.delete()` 的签名是 `-> None`，而且
        doc_id 不存在时会静默 return（不报错也不返回 False）。
        门面这里先查一次存在性，把「删成功」和「本来就没有」区分开——
        调用方（比如记忆巩固）需要知道到底发生了什么。
        """
        store = self.db.document_storage
        existing = self._bridge.run(store.get_document_by_doc_id(doc_id))
        if not existing:
            return False
        self._bridge.run(self.db.delete(doc_id))
        return True

    def rebuild_index(self) -> None:
        """从 `documents` 全量重建 FTS 索引。索引坏了不丢记忆。

        ⚠ 2026-11-04（T-06）：这个名字**长期在骗人** —— 它只重建 FTS，
        **不重建 faiss 向量索引**。当初就是因为它听着像"全量重建"，
        真出了不一致（49 条孤儿向量）也没人想到用它修。现在拆开：
        想修 FTS 用 `rebuild_fts()`，想修向量用 `rebuild_vectors()`。
        本方法保留为 FTS 的别名，避免现有调用方断掉。
        """
        self.rebuild_fts()

    def rebuild_fts(self) -> None:
        """只重建 FTS5 全文索引（不动 faiss）。"""
        self._bridge.run(self.db.document_storage.rebuild_fts_index())

    def rebuild_vectors(self) -> dict:
        """从 `documents` 全量重建 faiss 向量索引。

        ⚠ 2026-11-09 补：这个函数**一直只活在注释里**。
        `rebuild_index()` 的 docstring 早就写着「想修向量用 `rebuild_vectors()`」，
        但那个方法从来没有被实现过 —— 所以真出了索引不一致，
        看着注释去找工具，找不到。

        这是第二次栽在同一件事上（上一次 49 条孤儿、这次 56 条），
        两次都是手写一次性脚本修的。补上它，别再靠临场发挥。

        和 `drop_orphan_vectors()` 的分工：

          `drop_orphan_vectors` 只删多余的（索引有、库里没有），
                                便宜，不动已有向量。
          `rebuild_vectors`     两边都补齐：孤儿删掉、缺失的重算。
                                会把**所有**向量重算一遍，慢
                                （7 条文档在 CPU 上约 15 秒，1000 条约 20 分钟）。

        Returns:
            `{"before": 对账结果, "after": 对账结果, "rebuilt": 重算条数}`
        """
        before = self.consistency()

        docs = self._bridge.run(
            self.db.document_storage.get_documents({}, limit=None)
        ) or []
        if not docs:
            # 库是空的：直接清空索引，别留一堆孤儿向量在里头
            self._bridge.run(self.db.embedding_storage.delete(
                self.db.embedding_storage.all_ids()
            ))
            return {"before": before, "after": self.consistency(), "rebuilt": 0}

        import numpy as np  # noqa: PLC0415 —— 只有这里用

        # ① 先清空索引：全量重建，不做增量合并，
        #    这样"索引里多出来的"和"少掉的"一次全解决。
        old_ids = self.db.embedding_storage.all_ids()
        if old_ids:
            self._bridge.run(self.db.embedding_storage.delete(old_ids))

        # ② 逐条重算向量。
        #
        # ⚠ `embedding_storage.insert()` 的签名是 `(vector, id)` —— 它收的
        # **已经是向量**，不是文本。文本要先过 `self._embedding.get_embedding()`。
        # 这里一次性 batch 算完再逐条插，比一条一条掉进事件循环里算快得多。
        usable = [(int(d["id"]), str(d.get("text") or "").strip())
                  for d in docs
                  if d.get("id") is not None and str(d.get("text") or "").strip()]
        rebuilt = 0
        if usable:
            try:
                vecs = self._bridge.run(
                    self._embedding.get_embeddings([t for _, t in usable])
                )
            except Exception as exc:  # noqa: BLE001 —— 整批失败就整批放弃
                logger.warning("重建向量：算 embedding 失败，索引已清空待重试: %s", exc)
                vecs = []

            for (doc_id, _text), vec in zip(usable, vecs or []):
                try:
                    arr = np.asarray(vec, dtype=np.float32)
                    norm = float(np.linalg.norm(arr))
                    if norm > 0:
                        arr = arr / norm
                    self._bridge.run(
                        self.db.embedding_storage.insert(arr, doc_id)
                    )
                    rebuilt += 1
                except Exception as exc:  # noqa: BLE001 —— 单条失败不拦整批
                    logger.warning("重建向量失败 doc_id=%s: %s", doc_id, exc)

        if rebuilt:
            self._bridge.run(self.db.embedding_storage.save_index())

        after = self.consistency()
        logger.info(
            "重建向量索引：重算 %d/%d 条，前后 orphans %d->%d，missing %d->%d",
            rebuilt, len(usable),
            len(before["orphans"]), len(after["orphans"]),
            len(before["missing"]), len(after["missing"]),
        )
        return {"before": before, "after": after, "rebuilt": rebuilt}

    def consistency(self) -> dict:
        """对账：`documents` 行数 vs faiss 向量数。

        Returns:
            `{"docs": n, "vectors": m, "orphans": [...], "missing": [...], "ok": bool}`

        ⚠ 为什么必须有（T-06）：`documents` 和 faiss **无法共用一个事务**
        （`vec_db.py:204-227` 自己承认了，只能写补偿逻辑）。补偿失败的场景
        （进程被杀、断电）会把两边永久劈开，而**不报任何错**——
        召回只是悄悄变差。实测教训：documents 7 行、index 56 个向量，
        49 条孤儿把 top-k 名额占满，语义召回命中真记忆 **0 条**。
        """
        docs = self._bridge.run(self.db.document_storage.get_documents({}, limit=None)) or []
        doc_ids = {int(d["id"]) for d in docs if d.get("id") is not None}
        storage = self.db.embedding_storage
        vectors = storage.all_ids()
        vec_ids = set(vectors)
        orphans = sorted(vec_ids - doc_ids)
        missing = sorted(doc_ids - vec_ids)
        return {
            "docs": len(doc_ids),
            "vectors": len(vectors),
            "orphans": orphans,
            "missing": missing,
            "ok": not orphans and not missing,
        }

    def drop_orphan_vectors(self) -> int:
        """删掉索引里那些 `documents` 已经没有的向量。返回删掉几个。

        这是那次事故的**修复动作**：documents 删了、faiss 没删 →
        孤儿向量在 `retrieve()` 里照样被取出来，然后 `get_documents(ids=...)`
        查不到、被 `continue` 跳过 —— 名额白占，等于召回被稀释。
        """
        check = self.consistency()
        orphans = check["orphans"]
        if not orphans:
            return 0
        self._bridge.run(self.db.embedding_storage.delete(orphans))
        logger.warning("清理孤儿向量 %d 个（documents 里已不存在）", len(orphans))
        return len(orphans)

    def health(self) -> dict:
        """体检用的一行摘要（给 `system._health_check` 调，不要抛）。"""
        try:
            check = self.consistency()
        except Exception as exc:  # noqa: BLE001 —— 体检读不到不该炸
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            "ok": check["ok"],
            "docs": check["docs"],
            "vectors": check["vectors"],
            "orphan_vectors": len(check["orphans"]),
            "missing_vectors": len(check["missing"]),
        }

    # ------------------------------------------------------------------ 遗忘
    def prune(
        self,
        older_than_days: float = 90.0,
        min_importance: float = 0.3,
        dry_run: bool = False,
    ) -> dict:
        """遗忘：删掉「够旧 **且** 不重要」的记忆。返回统计。

        为什么需要（2026-11-03）
        -----------------------
        插件停用前靠它的 `cleanup_days_threshold=30` 兜底；插件停了以后
        `gateway/data/livingmemory.db` **只增不减**。后果不是"记得太多"，
        而是三年前的"今天很累"和今天的同样会被召回，`top_k=3` 的名额被陈旧记忆占掉。

        **双条件是硬的**：两个条件都满足才删。
        只看年龄会把「生日是10月20日」这种珍贵事实删掉；
        只看重要度会留下大量低分但很新的记忆。
        模型给的 importance 通常在 0.5~0.9，所以 `min_importance` 默认 0.3
        实际上只清真正的边角料。

        Args:
            older_than_days: 超过这么多天才可能被删。
            min_importance: 重要度**低于**这个值才可能被删。
            dry_run: 只统计不真删。

        Returns:
            `{"scanned":n, "matched":m, "deleted":d, "kept_important":k}`
        """
        cutoff = time.time() - max(0.0, float(older_than_days)) * 86400
        docs = self._bridge.run(self.db.document_storage.get_documents({}, limit=2000)) or []
        scanned = len(docs)
        matched: list[str] = []
        kept_important = 0

        for doc in docs:
            meta = _as_dict(doc.get("metadata"))
            try:
                importance = float(meta.get("importance", 0.5))
            except (TypeError, ValueError):
                importance = 0.5
            created = meta.get("create_time")
            try:
                created_ts = float(created) if created is not None else 0.0
            except (TypeError, ValueError):
                created_ts = 0.0

            # 拿不到时间就**不删**（fail-safe：宁可留，不可误删）
            if created_ts <= 0 or created_ts > cutoff:
                continue
            if importance >= float(min_importance):
                kept_important += 1
                continue
            matched.append(str(doc.get("doc_id") or ""))

        deleted = 0
        if not dry_run:
            for doc_id in matched:
                if doc_id and self.delete(doc_id):
                    deleted += 1
        return {
            "scanned": scanned,
            "matched": len(matched),
            "deleted": deleted,
            "kept_important": kept_important,
            "dry_run": bool(dry_run),
        }


def open_store(
    db_path: str,
    index_path: str | None = None,
    dim: int = DEFAULT_DIM,
    embedding=None,
) -> MemoryStore:
    """便捷构造 + open。"""
    return MemoryStore(
        db_path=db_path,
        index_path=index_path,
        dim=dim,
        embedding=embedding,
    ).open()
