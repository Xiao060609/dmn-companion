# -*- coding: utf-8 -*-
"""本地 embedding：bge-large-zh-v1.5（CPU）。

为什么加这个
------------
原来向量化走 DashScope `text-embedding-v4`（联网 + 按次计费）。换成
bge-large-zh-v1.5 后：
  * **零成本**（不再按次付费）
  * **不出机器**（记忆正文不再发给云端）
  * 维度仍是 **1024**，和 `DEFAULT_DIM` 一致，faiss 索引结构不用改

⚠ **和 faiss-gpu 的关系**：faiss 只负责"向量检索"，bge 负责"把文本变成向量"。
真正花钱的是后者。faiss 在当前数据量（几十条）用 CPU Flat 是微秒级，
换 GPU 零收益，所以本方案**不装 faiss-gpu**。

⚠ **bge 必须加查询前缀**（官方要求，漏了召回质量明显下降）：
  * 检索（query）侧：加 `"为这个句子生成表示以用于检索相关文章："`
  * 入库（document）侧：**不加**
这是 bge 系列和普通 sentence-transformers 模型最大的用法差异。

设计取舍
--------
* **懒加载**：第一次真要用才 import torch / 载模型（占几百 MB 内存，启动时不该背）。
* **单例**：模型文件 1.3GB，反复加载会让每次召回多等 3~5 秒。
* **同步模型 + 异步接口**：`model.encode()` 是同步且吃 CPU 的，必须丢到
  线程池，不能阻塞网关主线程（否则一轮对话会卡住整个 HTTP 服务）。
* **失败不抛穿**：向量化失败应该让调用方降级（关键词检索仍可用），
  而不是把整轮对话炸掉。
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger("xiaoyu.embedding")

#: bge 系列的查询指令前缀（官方要求，只加在 query 侧）。
BGE_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："

#: bge-large-zh-v1.5 的输出维度。必须和 faiss 索引维度一致。
DEFAULT_DIM = 1024

#: 默认模型目录。放项目外，避免 1.3GB 进 git。
DEFAULT_MODEL_DIR = r"G:\models"


class LocalEmbedding:
    """bge-large-zh-v1.5 的 `EmbeddingProvider` 实现（鸭子类型，不继承）。

    方法名与 `provider.EmbeddingProvider` 完全一致，所以
    `MemoryStore(embedding=LocalEmbedding())` 可以直接用。
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-large-zh-v1.5",
        cache_dir: str | None = None,
        device: str = "cpu",
        dim: int = DEFAULT_DIM,
        query_prefix: str = BGE_QUERY_PREFIX,
    ) -> None:
        self._model_name = model_name
        self._cache_dir = cache_dir or DEFAULT_MODEL_DIR
        self._device = device
        self._dim = int(dim)
        self._query_prefix = query_prefix
        self._model = None
        self._lock = threading.Lock()
        self._error: str | None = None

    # ---------------------------------------------------------------- 模型加载

    def _ensure_model(self):
        """懒加载模型（线程安全，失败只记一次不反复重试）。"""
        if self._model is not None:
            return self._model
        if self._error is not None:
            # 上次已经失败过，不要再花几秒重试一遍
            raise RuntimeError(self._error)
        with self._lock:
            if self._model is not None:
                return self._model
            if self._error is not None:
                raise RuntimeError(self._error)
            try:
                from sentence_transformers import SentenceTransformer

                os.makedirs(self._cache_dir, exist_ok=True)
                logger.info("加载本地 embedding 模型 %s（首次约 3~5 秒）", self._model_name)
                self._model = SentenceTransformer(
                    self._model_name,
                    cache_folder=self._cache_dir,
                    device=self._device,
                )
                # sentence-transformers 6.x 把 get_sentence_embedding_dimension
                # 改名成 get_embedding_dimension（旧的仍在但会告警）。
                # 两个都认，免得跟着上游版本抖动而崩。
                if hasattr(self._model, "get_embedding_dimension"):
                    got = int(self._model.get_embedding_dimension())
                else:
                    got = int(self._model.get_sentence_embedding_dimension())
                if got != self._dim:
                    # 维度不一致会让 faiss 直接报错，宁可早失败
                    raise RuntimeError(
                        f"模型维度 {got} 与配置 {self._dim} 不一致 —— "
                        f"faiss 索引维度不可变，换模型必须重建索引"
                    )
                logger.info("本地 embedding 就绪（dim=%d, device=%s）", self._dim, self._device)
                return self._model
            except Exception as exc:  # noqa: BLE001
                self._error = f"{type(exc).__name__}: {exc}"
                logger.warning("本地 embedding 加载失败: %s", self._error)
                raise

    # ---------------------------------------------------------------- 核心接口

    def get_dim(self) -> int:
        return self._dim

    def _encode(self, texts: list[str], *, is_query: bool) -> list[list[float]]:
        """同步编码。`is_query=True` 时加 bge 的检索前缀。"""
        model = self._ensure_model()
        if is_query:
            texts = [self._query_prefix + t for t in texts]
        # normalize_embeddings=True：bge 官方建议归一化后用内积/余弦检索，
        # 和 faiss 那边的 IndexFlatIP 用法一致
        vecs = model.encode(
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [v.tolist() for v in vecs]

    async def get_embedding(self, text: str) -> list[float]:
        """单条文本 → 向量。

        ⚠ 这里**不加**查询前缀：这个方法在写入路径上被调用得最多
        （`store.add` → `vec_db.insert`）。检索路径走的是
        `get_embeddings_batch(is_query=True)`，见 `search_query`。
        """
        return (await self.get_embeddings([text]))[0]

    async def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        """批量 → 向量（不含查询前缀，写入路径用）。"""
        import asyncio

        return await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._encode(list(texts), is_query=False)
        )

    async def get_embeddings_batch(
        self, texts: list[str], batch_size: int = 32
    ) -> list[list[float]]:
        """分批（写入大量记忆时用）。当前实现直接一次做完 —— 数据量小。"""
        return await self.get_embeddings(texts)

    async def embed_query(self, text: str) -> list[float]:
        """**检索专用**：带 bge 查询前缀。

        ⚠ 这是本类与 `DashScopeEmbedding` 的唯一语义差异。检索入口
        （`MemoryStore.search` / `search_hybrid`）应当走这个，
        写入路径走 `get_embedding`。混用会让召回质量下降。
        """
        import asyncio

        return (
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._encode([text], is_query=True)
            )
        )[0]

    # ---------------------------------------------------------------- 健康检查

    def health(self) -> dict[str, Any]:
        """给体检用：模型能不能加载、维度对不对。**不抛**。"""
        try:
            self._ensure_model()
            return {"ok": True, "model": self._model_name, "dim": self._dim,
                    "device": self._device, "cached": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "model": self._model_name,
                    "error": f"{type(exc).__name__}: {exc}"}
