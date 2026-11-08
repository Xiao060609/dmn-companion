"""小鱼记忆层（自主实现）。

来源
----
本目录的代码从 AstrBot 的 `FaissVecDB` 与 `livingmemory` 插件剥离而来，
**表结构保持 1:1**，所以插件的现有数据可以直接读（已验证 13/13 逐条一致）。

设计取舍
--------
* 只搬「存储 + 检索」，不搬插件的陪伴人格体系（companion）、WebUI 后端（page_api）。
* 表名 / 列名 / FTS 表名与原插件完全一致，避免迁移。
* 依赖：faiss + numpy + sqlalchemy + sqlmodel + aiosqlite + jieba（+ greenlet）。

模块
----
    base.py                  Result / BaseVecDB（零依赖）
    tokenizer.py             jieba 分词 + FTS5 查询构造（零依赖）
    embedding_storage.py     faiss 索引读写（自带中文路径 ASCII 桥接）
    document_storage.py      documents 表 + FTS5 全文索引
    vec_db.py                FaissVecDB：向量 + 文档 + 混合检索
    provider.py              Provider 协议（鸭子类型）
    dashscope_embedding.py   DashScope 文本向量化
    loop_bridge.py           同步→异步桥（常驻事件循环）
    store.py                 MemoryStore 门面（本包推荐入口）

⚠ 调用约定（踩过的坑）
--------------------
`DocumentStorage.search_sparse(query_tokens, limit)` 收的是**已分词列表**，
不是原始字符串。传字符串会被 `build_fts5_or_query` 逐字符遍历
（`"人群"` → `"人" OR "群"`），结果看似"检索不准"，其实是调错了。

正确写法：
    from tokenizer import to_fts5_search_text
    tokens = to_fts5_search_text(query, store.stopwords).split()
    hits = store.search_sparse(tokens, limit=5)
"""
from __future__ import annotations

__all__ = [
    "MemoryStore",
    "open_store",
]

__version__ = "0.1.0"


def __getattr__(name: str):
    """惰性导出 `MemoryStore` / `open_store`。

    为什么惰性：`store.py` 会 import sqlalchemy / faiss 这些重依赖。
    只 `import memory.provider` 做类型检查时，不该被迫加载整个向量栈。
    """
    if name in ("MemoryStore", "open_store"):
        from store import MemoryStore, open_store  # noqa: PLC0415

        return {"MemoryStore": MemoryStore, "open_store": open_store}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
