# -*- coding: utf-8 -*-
"""P0-5：认知库「先读再写」+ 反刍不进记忆库（2026-11-07）。

主人的两条定稿：
  ① **模型有智慧，框架不该替它定规则** —— 巩固时把「已经存好的认知」
     喂给它，它自己判断该不该再写一条。
  ② **反刍/预演的产物只进 inner_os 想法队列，不进长期记忆库** ——
     那两类的 prompt 要求的就是场景推断（「他话说得少，可能累了」），
     是想法不是事实；进了库会持续污染召回。

这两条用例守的是「机制真的接上了」，不是「函数返回值对不对」。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
MEMORY_DIR = GATEWAY_DIR / "memory"
# memory 包内部用的是扁平 import（`store.py` 里 `from dashscope_embedding import ...`），
# 所以 memory 目录本身也要进 sys.path —— 和 test_memory_bridge.py 一致。
for _p in (str(GATEWAY_DIR), str(MEMORY_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import system  # noqa: E402

DIM = 1024


class FakeEmbedding:
    """不花 API 额度的向量化实现（照抄 `test_daily_review_sinks.py`）。

    ⚠ 本文件要用 `store.add()`，而 `store.py` 在没传 embedding 时会默认
    建一个**真的** `DashScopeEmbedding` —— 那会用真 key 打阿里云，
    而且测试文本会离开本机。conftest 管得住文件路径，管不住出网点。
    """

    def __init__(self, dim: int = DIM) -> None:
        self._dim = dim

    def get_dim(self) -> int:
        return self._dim

    def _vec(self, text: str) -> list[float]:
        import numpy as np

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


class _StubBridge:
    """把 `system._memory_bridge()` 换成一个喂了 FakeEmbedding 的桥。

    为什么不 monkeypatch `open_store`：`system._memory_bridge()` 内部自己 new
    一个 `MemoryBridge(db_path=...)`，而 `MemoryBridge.__init__` **不接受 embedding
    参数** —— 它把这件事推给 `MemoryStore.store()`，那里才决定用哪个 provider。
    所以从 `open_store` 那一层拦截不到 `system` 这条调用链。
    直接换 bridge 更直白，也更能证明"system 真的用了我们给的这个 store"。
    """

    def __init__(self, store):
        self._store = store

    def store(self):
        return self._store

    def write_thoughts(self, texts, channel, *, origin="", cognition_only=True,
                       importance=0.55):
        written = 0
        import cognition
        session_id = "owner"
        for txt in texts:
            body = str(txt or "").strip()
            if not body:
                continue
            if cognition_only and not cognition.judge(body).get("is_cognition"):
                continue
            self._store.add(
                text=body + "；梳理期念头；分寸",
                metadata={"session_id": session_id, "source_channel": str(channel or ""),
                          "importance": importance, "memory_origin": origin,
                          "kind": "observation"},
            )
            written += 1
        return written


@pytest.fixture(autouse=True)
def offline_memory(monkeypatch):
    """整个文件都不许联网：用喂了 FakeEmbedding 的 store 替换 system 的桥。"""
    import store as store_mod

    db_path = os.path.join(system.DATA_DIR, "livingmemory.db")
    st = store_mod.open_store(db_path=db_path, dim=DIM, embedding=FakeEmbedding())
    stub = _StubBridge(st)
    monkeypatch.setattr(system, "_memory_bridge", lambda: stub, raising=False)
    yield stub


@pytest.fixture()
def store(offline_memory):
    return offline_memory._store


def _seed(store, texts):
    for t in texts:
        store.add(text=t, metadata={"session_id": "owner", "kind": "cognition",
                                    "source_channel": "pet", "importance": 0.6})


def test_已有认知能被读出来(tmp_path, monkeypatch):
    """核心：记忆库里有什么，能读到。"""
    store = system._memory_bridge().store()
    _seed(store, ["他喜欢蓝色；喜欢；蓝色"])
    got = system._read_existing_cognitions()
    assert "他喜欢蓝色" in got


def test_读出来的是叙述句而不是带关键词的富文本(tmp_path):
    """写入形状是「叙述句；关键词」，喂给模型时只该给叙述句。

    关键词是给 FTS 用的，给模型看是噪音，还可能诱导它照抄。
    """
    store = system._memory_bridge().store()
    _seed(store, ["主人讨厌人群和应酬；讨厌人群；应酬；社交"])
    got = system._read_existing_cognitions()
    assert "主人讨厌人群和应酬" in got
    assert not any("；" in g for g in got), "不该把关键词部分也喂给模型"


def test_空库返回空列表而不是报错():
    """清空记忆库后，巩固还得能正常跑。"""
    assert system._read_existing_cognitions() == []


def test_读不到库时返回空列表不抛(monkeypatch):
    """读不到就不喂 —— 不能因为这一步炸掉整轮巩固。"""
    monkeypatch.setattr(system, "_memory_bridge", lambda: None)
    assert system._read_existing_cognitions() == []


def test_巩固payload里真的带上了已有认知():
    """端到端：拼出来的 payload 里必须有这个字段。

    只测 `_read_existing_cognitions()` 返回对是不够的 ——
    真正的 bug 是"函数写好了但没接进 payload"（上一轮的 P0-5 就是这么来的）。
    """
    import json

    store = system._memory_bridge().store()
    _seed(store, ["他喜欢蓝色；喜欢；蓝色"])
    st = system.load_state()
    turns = [{"role": "user", "content": "主人: 今天好累", "ts": 0}]
    msgs = system._build_consolidate_messages(
        st, turns, channel=system.CHANNEL_OWNER_KEY)
    payload = json.loads(msgs[-1]["content"])
    assert "existing_cognitions" in payload
    assert "他喜欢蓝色" in payload["existing_cognitions"]


def test_群渠道不喂主人私聊的认知():
    """认知库按 session_id 隔离，群渠道不该看到主人的认知。"""
    import json

    store = system._memory_bridge().store()
    _seed(store, ["主人讨厌人群和应酬；讨厌人群；应酬"])
    st = system.load_state()
    turns = [{"role": "user", "content": "【群聊·某群：10000002】群友（路人）： 在吗", "ts": 0}]
    msgs = system._build_consolidate_messages(
        st, turns, channel="qq_group:某群")
    payload = json.loads(msgs[-1]["content"])
    assert "existing_cognitions" not in payload, (
        "群渠道不该拿到主人的长期认知 —— 那是跨场合泄漏")


def test_上限常量生效():
    assert system.EXISTING_COGNITIONS_LIMIT > 0
    got = system._read_existing_cognitions(limit=1)
    assert len(got) <= 1


# ------------------------------------------------------------ 反刍不进记忆库

def test_记忆沉淀开关默认关闭():
    """P0-5 的核心：反刍/预演默认不再写进记忆库。"""
    cfg = system._daily_review_cfg()
    assert cfg.get("memory_sink_enabled") is False, (
        "memory_sink_enabled 默认必须为 False —— "
        "反刍产物是想法不是事实，进库会污染召回")


def test_开关关着时反刍产物不写库():
    """模拟 daily_review 的 sink 段：开关关闭 → 一条都不写。"""
    store = system._memory_bridge().store()
    before = len(store.list_documents())
    cfg = system._daily_review_cfg()
    sink_texts = [("ruminate", ["他嘴硬说没不高兴，其实是在试探我"]),
                  ("prospect", ["明天该问问他睡够没"])]
    wrote = 0
    if cfg.get("memory_sink_enabled", False):
        for activity, texts in sink_texts:
            if activity not in ("ruminate", "prospect"):
                continue
            wrote += system._memory_bridge().write_thoughts(
                texts, "pet", origin="daily_review")
    assert wrote == 0
    assert len(store.list_documents()) == before
