# -*- coding: utf-8 -*-
"""认知合并写（2026-11-07，主人定的方案）。

流程：
    新认知诞生 → 向量库匹配旧认知 → 旧认知转文本
    → 模型读「旧+新」按规则输出最终文本 → 新文本向量化后注入库

铁律：
  1. **先插新的，成功后删旧的** —— 反过来的话 embedding 一失败两条都丢。
  2. **解析失败/模型没答 → 按纯新增处理** —— 宁可重复，不可误删。
  3. 只和**同一会话**的旧认知合并 —— owner 的认知不能和群聊的混。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
MEMORY_DIR = GATEWAY_DIR / "memory"
for _p in (str(GATEWAY_DIR), str(MEMORY_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import memory_bridge  # noqa: E402

DIM = 1024


class FakeEmbedding:
    def __init__(self, dim: int = DIM) -> None:
        self._dim = dim

    def get_dim(self) -> int:
        return self._dim

    def _vec(self, text: str) -> list[float]:
        rng = np.random.default_rng(abs(hash(text)) % (2**32))
        v = rng.random(self._dim).astype(np.float32)
        v /= np.linalg.norm(v)
        return v.tolist()

    async def get_embedding(self, text: str) -> list[float]:
        return self._vec(text)

    async def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    async def get_embeddings_batch(self, texts, batch_size: int = 32):
        return await self.get_embeddings(texts)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """一个隔离的、不联网的库。"""
    import store as store_mod

    st = store_mod.open_store(
        db_path=str(tmp_path / "livingmemory.db"),
        dim=DIM,
        embedding=FakeEmbedding(),
    )
    return st


def _seed(store, text, session="owner", kind="cognition"):
    store.add(text=text, metadata={"session_id": session, "kind": kind,
                                   "source_channel": "pet", "importance": 0.6})


def _texts(store):
    return [str(d.get("text") or "").split("；", 1)[0] for d in store.list_documents()]


# ------------------------------------------------------------ 解析器（纯函数）

def test_解析_同一件事合成一条():
    rel = [{"doc_id": "A", "text": "他喜欢蓝色"}]
    got = memory_bridge._parse_merge_reply("他喜欢深蓝色\nDROP: 1", rel)
    assert got == ("他喜欢深蓝色", ["A"])


def test_解析_不要删任何旧的():
    rel = [{"doc_id": "A", "text": "他喜欢蓝色"}]
    got = memory_bridge._parse_merge_reply("他最近总找人聊天\nDROP: 无", rel)
    assert got == ("他最近总找人聊天", [])


def test_解析_只有DROP没有正文_判失败():
    """宁可重复，不可误删。"""
    rel = [{"doc_id": "A", "text": "他喜欢蓝色"}]
    assert memory_bridge._parse_merge_reply("DROP: 1", rel) is None


def test_解析_空回复判失败():
    assert memory_bridge._parse_merge_reply("", [{"doc_id": "A"}]) is None


def test_解析_越界编号被忽略():
    """模型编了个不存在的编号 → 忽略，不误删别的东西。"""
    rel = [{"doc_id": "A", "text": "x"}]
    got = memory_bridge._parse_merge_reply("新的说法\nDROP: 9", rel)
    assert got is not None
    assert got[1] == []


def test_解析_多个编号():
    rel = [{"doc_id": "A"}, {"doc_id": "B"}, {"doc_id": "C"}]
    got = memory_bridge._parse_merge_reply("合并后\nDROP: 1,3", rel)
    assert got == ("合并后", ["A", "C"])


# ------------------------------------------------------------ 合并写（端到端）

def test_合并_同会话旧认知被替换(store, monkeypatch):
    """核心：旧的和新的说同一件事 → 只留一条。"""
    _seed(store, "他喜欢蓝色；喜欢；蓝色")

    monkeypatch.setattr(
        memory_bridge.MemoryBridge, "_ask_model_to_merge",
        lambda self, new, rel: ("他喜欢深蓝色，尤其藏青色；喜欢；深蓝色", [rel[0]["doc_id"]]),
    )
    bridge = memory_bridge.MemoryBridge(db_path="x")
    bridge._store = store

    ok = bridge._merge_into_memory(
        store, "他喜欢蓝色；喜欢；蓝色",
        {"session_id": "owner", "kind": "cognition"},
    )
    assert ok
    got = _texts(store)
    assert len(got) == 1, f"应该只剩一条，实际: {got}"
    assert "深蓝色" in got[0]


def test_合并_模型没答时退化为纯新增(store, monkeypatch):
    """宁可重复，不可误删。"""
    _seed(store, "他喜欢蓝色；喜欢；蓝色")
    monkeypatch.setattr(
        memory_bridge.MemoryBridge, "_ask_model_to_merge",
        lambda self, new, rel: None,
    )
    bridge = memory_bridge.MemoryBridge(db_path="x")
    bridge._store = store
    bridge._merge_into_memory(store, "他喜欢蓝色；喜欢；蓝色",
                              {"session_id": "owner", "kind": "cognition"})
    got = _texts(store)
    assert len(got) == 2, "模型没答就该新增，不能删旧的"


def test_合并_插入失败时旧数据完好(store, monkeypatch):
    """铁律：先插后删。插入炸了，旧的必须还在。"""
    _seed(store, "他喜欢蓝色；喜欢；蓝色")
    before = _texts(store)

    monkeypatch.setattr(
        memory_bridge.MemoryBridge, "_ask_model_to_merge",
        lambda self, new, rel: ("新的说法；x；y", [rel[0]["doc_id"]]),
    )

    real_add = store.add

    def boom(*a, **k):
        raise RuntimeError("模拟 embedding 失败")

    monkeypatch.setattr(store, "add", boom)

    bridge = memory_bridge.MemoryBridge(db_path="x")
    bridge._store = store
    with pytest.raises(RuntimeError):
        bridge._merge_into_memory(store, "他喜欢蓝色；喜欢；蓝色",
                                  {"session_id": "owner", "kind": "cognition"})
    monkeypatch.setattr(store, "add", real_add)
    assert _texts(store) == before, "插入失败时旧认知必须完好无损"


def test_合并不跨会话(store, monkeypatch):
    """owner 的认知不能和群聊的合并（跨场合泄漏）。"""
    _seed(store, "他喜欢蓝色；喜欢；蓝色", session="qq_group:某群")
    called = {"n": 0}

    def spy(self, new, rel):
        called["n"] += 1
        return ("错误地合并了", [rel[0]["doc_id"]])

    monkeypatch.setattr(memory_bridge.MemoryBridge, "_ask_model_to_merge", spy)
    bridge = memory_bridge.MemoryBridge(db_path="x")
    bridge._store = store
    bridge._merge_into_memory(store, "他喜欢蓝色；喜欢；蓝色",
                              {"session_id": "owner", "kind": "cognition"})
    # 同会话过滤发生在调用模型之前，所以这条不该触发合并判断
    assert called["n"] == 0, "不该拿别的会话的旧认知去合并"
    assert len(_texts(store)) == 2, "群聊那条保持不动，owner 那条正常新增"
