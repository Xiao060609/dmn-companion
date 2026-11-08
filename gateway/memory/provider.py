"""向量/重排 Provider 接口 —— 替代 AstrBot 的 `EmbeddingProvider` / `RerankProvider`。

为什么要自己写
--------------
`vec_db.py` / `document_storage.py` 原来从 `astrbot.core.provider.provider` 拿这两个
抽象基类。网关不依赖 AstrBot，所以这里用 **Protocol（结构化子类型）** 重新声明：
只要你的对象有这几个方法，就自动算作 provider，不需要继承。

这比继承 ABC 更宽松，也更好测（测试里塞个假实现即可）。

真正的实现见 `dashscope_embedding.py`。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    """把文本变成向量。

    与 AstrBot 的 `EmbeddingProvider` 方法名保持一致（`get_embedding` /
    `get_embeddings` / `get_dim`），这样搬过来的代码不用改调用点。
    """

    def get_dim(self) -> int:
        """向量维度。必须在建索引之前确定（faiss 索引维度不可变）。"""
        ...

    async def get_embedding(self, text: str) -> list[float]:
        """单条文本 → 向量。"""
        ...

    async def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        """批量文本 → 向量列表（顺序与入参一致）。"""
        ...

    async def get_embeddings_batch(
        self,
        texts: list[str],
        batch_size: int = 32,
    ) -> list[list[float]]:
        """分批向量化（大批量时避免单次请求过大）。"""
        ...


@runtime_checkable
class RerankProvider(Protocol):
    """重排（可选）。不配置时 `FaissVecDB` 走纯向量/BM25 融合。"""

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int | None = None,
    ) -> list[dict[str, Any]]:
        """返回 `[{"index": int, "relevance_score": float}, ...]`，按相关度降序。"""
        ...


class NullRerankProvider:
    """不重排的占位实现：原样返回、分数置 0。

    为什么要有个"显式空实现"而不是传 None：
    `FaissVecDB.retrieve()` 里对 `rerank_provider` 有分支判断，
    显式空实现能让那条分支稳定走到，不用到处写 `if rerank_provider is not None`。
    """

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int | None = None,
    ) -> list[dict[str, Any]]:
        limit = top_n if top_n is not None else len(documents)
        return [
            {"index": i, "relevance_score": 0.0}
            for i in range(min(limit, len(documents)))
        ]
